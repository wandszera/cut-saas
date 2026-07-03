import unittest
from datetime import datetime, timedelta, UTC
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db.database import Base, get_db
from app.main import app
from app.models.candidate import Candidate
from app.models.clip import Clip
from app.models.job import Job
from app.models.niche_definition import NicheDefinition
from app.models.job_step import JobStep
from app.models.subscription import Subscription
from app.services.accounts import create_user_with_workspace
from app.services.auth import create_session_token
from app.services.llm_provider import LLMRateLimitError
from app.services.niche_registry import create_pending_niche
from app.services.pipeline import MAX_STEP_ATTEMPTS, process_job_pipeline
from app.services.candidates import _get_mode_candidate_limits
from app.services.segmentation import split_segments_into_time_chunks
from tests.routes.base import RoutesTestCase

class TestWebJobDetail(RoutesTestCase):
    def test_web_job_creation_redirects_and_runs_background_pipeline(self):
        with patch('app.web.pages.actions.fetch_youtube_metadata', return_value={'duration_seconds': 1200}), patch('app.web.pages.actions.enqueue_pipeline_job') as mocked_enqueue:
            response = self.client.post('/web/jobs/create', data={'url': 'https://www.youtube.com/watch?v=abc123def45'}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/jobs/1/view')
        mocked_enqueue.assert_called_once()
        self.assertEqual(mocked_enqueue.call_args.args[1], 1)
        self.assertEqual(mocked_enqueue.call_args.kwargs, {})
        db = self._session()
        try:
            job = db.query(Job).one()
            self.assertEqual(job.source_value, 'https://www.youtube.com/watch?v=abc123def45')
            self.assertEqual(job.status, 'pending')
        finally:
            db.close()


    def test_web_local_job_creation_redirects_and_runs_background_pipeline(self):
        with patch('app.web.pages.actions.probe_video_duration_seconds', return_value=1200), patch('app.web.pages.actions.enqueue_pipeline_job') as mocked_enqueue, patch('app.web.pages.helpers.settings.base_data_dir', str(self.test_artifacts_dir)):
            response = self.client.post('/web/jobs/create-local', data={'title': 'Upload externo'}, files={'video_file': ('video_form.mp4', b'fake-video', 'video/mp4')}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/jobs/1/view')
        mocked_enqueue.assert_called_once()
        self.assertEqual(mocked_enqueue.call_args.args[1], 1)
        self.assertEqual(mocked_enqueue.call_args.kwargs, {})
        db = self._session()
        try:
            job = db.query(Job).one()
            self.assertEqual(job.source_type, 'local')
            self.assertEqual(job.title, 'Upload externo')
            self.assertEqual(job.status, 'pending')
            self.assertTrue(job.source_value.endswith('_video_form.mp4'))
            self.assertEqual(job.source_value, job.video_path)
        finally:
            db.close()


    def test_web_job_creation_allows_single_trial_without_billing(self):
        db = self._session()
        try:
            db.query(Subscription).delete()
            db.commit()
        finally:
            db.close()
        with patch('app.web.pages.actions.fetch_youtube_metadata', return_value={'duration_seconds': 1200}), patch('app.web.pages.actions.enqueue_pipeline_job') as mocked_enqueue:
            response = self.client.post('/web/jobs/create', data={'url': 'https://www.youtube.com/watch?v=trial123'}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/jobs/1/view')
        mocked_enqueue.assert_called_once()


    def test_web_job_creation_blocks_second_trial_without_billing(self):
        db = self._session()
        try:
            db.query(Subscription).delete()
            db.commit()
        finally:
            db.close()
        self._create_job(status='done', title='Primeiro teste')
        with patch('app.web.pages.actions.fetch_youtube_metadata', return_value={'duration_seconds': 1200}):
            response = self.client.post('/web/jobs/create', data={'url': 'https://www.youtube.com/watch?v=second123'}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn('/billing', response.headers['location'])


    def test_web_job_creation_blocks_trial_video_longer_than_30_minutes(self):
        db = self._session()
        try:
            db.query(Subscription).delete()
            db.commit()
        finally:
            db.close()
        with patch('app.web.pages.actions.fetch_youtube_metadata', return_value={'duration_seconds': 1860}):
            response = self.client.post('/web/jobs/create', data={'url': 'https://www.youtube.com/watch?v=toolong123'}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn('/billing', response.headers['location'])


    def test_job_detail_page_renders_pipeline_section(self):
        job = self._create_job(status='failed')
        db = self._session()
        try:
            db.add(JobStep(job_id=job.id, step_name='transcribing', status='failed', attempts=2, error_message='erro de transcrição'))
            db.commit()
        finally:
            db.close()
        response = self.client.get(f'/jobs/{job.id}/view')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Pipeline', response.text)
        self.assertIn('transcribing', response.text)
        self.assertIn('erro de transcrição', response.text)
        self.assertIn('Reprocessar etapa', response.text)


    def test_job_detail_page_renders_step_observability_metadata(self):
        job = self._create_job(status='failed')
        db = self._session()
        try:
            db.add(JobStep(job_id=job.id, step_name='transcribing', status='failed', attempts=2, error_message='falha observada', details='{"attempt": 2, "duration_seconds": 1.234, "reason": "audio_missing", "audio_path": "C:/tmp/audio.mp3", "forced": true}'))
            db.commit()
        finally:
            db.close()
        response = self.client.get(f'/jobs/{job.id}/view')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Duração: 1.234s', response.text)
        self.assertIn('Motivo: audio_missing', response.text)
        self.assertIn('Execução forçada', response.text)
        self.assertIn('audio path:', response.text.lower())
        self.assertIn('C:/tmp/audio.mp3', response.text)


    def test_job_detail_page_renders_running_step_heartbeat_metadata(self):
        job = self._create_job(status='analyzing')
        db = self._session()
        try:
            db.add(JobStep(job_id=job.id, step_name='analyzing', status='running', attempts=1, details='{"attempt": 1, "progress_message": "Gerando insights da transcricao", "progress_percent": 64, "heartbeat_at": "2026-04-18T19:45:00+00:00"}'))
            db.commit()
        finally:
            db.close()
        response = self.client.get(f'/jobs/{job.id}/view')
        self.assertEqual(response.status_code, 200)
        self.assertIn('64%', response.text)
        self.assertIn('Gerando insights da transcricao', response.text)
        self.assertIn('2026-04-18T19:45:00+00:00', response.text)


    def test_job_detail_page_renders_partial_candidates_during_analyzing(self):
        job = self._create_job(status='analyzing')
        self._create_candidate(job.id, status='pending', score=9.4, opening_text='gancho parcial')
        response = self.client.get(f'/jobs/{job.id}/view')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Candidatos', response.text)
        self.assertIn('gancho parcial', response.text)


    def test_job_detail_page_flags_stale_running_step(self):
        job = self._create_job(status='analyzing')
        db = self._session()
        try:
            db.add(JobStep(job_id=job.id, step_name='analyzing', status='running', attempts=1, details='{"attempt": 1, "progress_message": "Gerando insights da transcricao", "heartbeat_at": "2026-04-18T18:00:00+00:00"}'))
            db.commit()
        finally:
            db.close()
        with patch('app.web.pages.helpers.datetime') as mocked_datetime:
            mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
            mocked_datetime.now.return_value = datetime.fromisoformat('2026-04-18T19:00:01+00:00')
            mocked_datetime.utcnow.return_value = datetime(2026, 4, 18, 19, 0, 1)
            response = self.client.get(f'/jobs/{job.id}/view')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Possivel travamento:', response.text)
        self.assertIn('sem nova atividade ha pelo menos 3601s', response.text)


    def test_job_detail_page_renders_conclude_without_llm_action_for_llm_step(self):
        job = self._create_job(status='analyzing')
        db = self._session()
        try:
            db.add(JobStep(job_id=job.id, step_name='llm_enrichment', status='running', attempts=1, details='{"attempt": 1, "progress_message": "Gerando insights da transcricao"}'))
            db.commit()
        finally:
            db.close()
        response = self.client.get(f'/jobs/{job.id}/view')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Concluir sem LLM', response.text)


    def test_job_detail_page_renders_feedback_learning_context(self):
        job = self._create_job(status='done', transcript_path='C:/tmp/transcript.json', detected_niche='podcast', transcript_insights='{"main_topics":["precificacao"],"viral_angles":["erro de margem"],"priority_keywords":["margem","preco"],"avoid_patterns":["contexto externo"],"promising_ranges":[{"start_hint_seconds":30,"end_hint_seconds":95,"why":"gancho forte"}]}')
        reference_job = self._create_job(status='done', detected_niche='podcast')
        self._create_candidate(job.id, status='approved', mode='short', full_text='resultado prÃƒÂ¡tico com exemplo claro', opening_text='resultado prÃƒÂ¡tico com exemplo claro', closing_text='esse ÃƒÂ© o ponto final.', reason='gancho forte, alinhado aos tópicos prioritários da transcrição, coincide com trecho promissor da análise global', transcript_context_score=1.7, llm_score=8.9, llm_why='tem clareza, promessa concreta e funciona sem contexto externo', llm_title='O erro de margem que derruba seu lucro', llm_hook='Se a sua margem parece boa mas o lucro some, esse é o motivo')
        self._create_candidate(reference_job.id, status='approved', mode='short', full_text='resultado prÃ¡tico com exemplo forte', hook_score=3.4, clarity_score=2.2, closure_score=2.0, emotion_score=1.1, duration_fit_score=4.0)
        self._create_candidate(reference_job.id, status='rendered', mode='short', full_text='resultado real com exemplo claro', hook_score=3.1, clarity_score=2.0, closure_score=1.9, emotion_score=1.0, duration_fit_score=4.1)
        ranked_candidates = [{'start': 12.0, 'end': 72.0, 'duration': 60.0, 'score': 9.6, 'reason': 'gancho forte', 'text': 'resultado prÃ¡tico com exemplo claro', 'opening_text': 'resultado prÃ¡tico com exemplo claro', 'closing_text': 'esse Ã© o ponto final.', 'feedback_alignment_score': 1.3}]
        response = self.client.get(f'/jobs/{job.id}/view')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Aprendizado', response.text)
        self.assertIn('Contexto da Transcricao', response.text)
        self.assertIn('precificacao', response.text)
        self.assertIn('00:30 -&gt; 01:35', response.text)
        self.assertIn('Base de feedback', response.text)
        self.assertIn('Aprovado', response.text)
        self.assertIn('resultado', response.text.lower())
        self.assertIn('alinhado ao contexto global', response.text)
        self.assertIn('coincide com trecho promissor', response.text)
        self.assertIn('Leitura do score', response.text)
        self.assertIn('Heuristico', response.text)
        self.assertIn('Contexto', response.text)
        self.assertIn('Final', response.text)
        self.assertIn('LLM muito confiante', response.text)
        self.assertIn('O erro de margem que derruba seu lucro', response.text)
        self.assertIn('Peso hibrido atual', response.text)


    def test_recalibrate_feedback_from_page_redirects_back_to_job(self):
        job = self._create_job(status='done', transcript_path='C:/tmp/transcript.json', detected_niche='podcast')
        with patch('app.web.pages.actions.learn_keywords_for_niche', return_value=[]) as mocked_learn:
            response = self.client.post(f'/jobs/{job.id}/view/feedback/recalibrate', data={'mode': 'short'}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers['location'].startswith(f'/jobs/{job.id}/view?mode=short'))
        self.assertIn('message=Aprendizado+recalibrado.', response.headers['location'])
        mocked_learn.assert_called_once()


    def test_candidate_editorial_actions_from_page_update_state(self):
        job = self._create_job()
        candidate = self._create_candidate(job.id, status='pending')
        approve_response = self.client.post(f'/jobs/{job.id}/view/candidates/{candidate.id}/status', data={'mode': 'short', 'status': 'approved'}, follow_redirects=False)
        self.assertEqual(approve_response.status_code, 303)
        favorite_response = self.client.post(f'/jobs/{job.id}/view/candidates/{candidate.id}/favorite', data={'mode': 'short'}, follow_redirects=False)
        self.assertEqual(favorite_response.status_code, 303)
        notes_response = self.client.post(f'/jobs/{job.id}/view/candidates/{candidate.id}/notes', data={'mode': 'short', 'editorial_notes': 'Abrir 2s antes e manter legenda.'}, follow_redirects=False)
        self.assertEqual(notes_response.status_code, 303)
        db = self._session()
        try:
            refreshed = db.query(Candidate).filter(Candidate.id == candidate.id).one()
            self.assertEqual(refreshed.status, 'approved')
            self.assertTrue(refreshed.is_favorite)
            self.assertEqual(refreshed.editorial_notes, 'Abrir 2s antes e manter legenda.')
        finally:
            db.close()


    def test_bulk_candidate_editorial_actions_from_page_update_state(self):
        job = self._create_job()
        first = self._create_candidate(job.id, status='pending', is_favorite=False)
        second = self._create_candidate(job.id, status='pending', is_favorite=False)
        approve_response = self.client.post(f'/jobs/{job.id}/view/candidates/bulk', data={'mode': 'short', 'bulk_action': 'approve', 'candidate_ids': [first.id, second.id]}, follow_redirects=False)
        self.assertEqual(approve_response.status_code, 303)
        favorite_response = self.client.post(f'/jobs/{job.id}/view/candidates/bulk', data={'mode': 'short', 'bulk_action': 'favorite_on', 'candidate_ids': [first.id, second.id]}, follow_redirects=False)
        self.assertEqual(favorite_response.status_code, 303)
        db = self._session()
        try:
            refreshed = db.query(Candidate).filter(Candidate.job_id == job.id).all()
            self.assertTrue(all((candidate.status == 'approved' for candidate in refreshed)))
            self.assertTrue(all((candidate.is_favorite for candidate in refreshed)))
        finally:
            db.close()


    def test_render_approved_from_page_creates_clips_and_marks_candidates_rendered(self):
        job = self._create_job()
        first = self._create_candidate(job.id, status='approved', is_favorite=True, start_time=10.0, end_time=70.0, duration=60.0)
        second = self._create_candidate(job.id, status='approved', start_time=90.0, end_time=150.0, duration=60.0)
    
        def render_side_effect(**kwargs):
            return f"C:/tmp/page_rendered_{kwargs['clip_index']}.mp4"
        with patch('app.services.render_workflow.render_clip', side_effect=render_side_effect):
            response = self.client.post(f'/jobs/{job.id}/view/render-approved', data={'mode': 'short', 'render_preset': 'impact'}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers['location'].startswith(f'/jobs/{job.id}/view?mode=short&render_preset=impact'))
        self.assertIn('message=Render+concluido+com+sucesso.', response.headers['location'])
        db = self._session()
        try:
            refreshed = {candidate.id: candidate.status for candidate in db.query(Candidate).filter(Candidate.job_id == job.id).all()}
            clips = db.query(Clip).filter(Clip.job_id == job.id).all()
            self.assertEqual(refreshed[first.id], 'rendered')
            self.assertEqual(refreshed[second.id], 'rendered')
            self.assertEqual(len(clips), 2)
        finally:
            db.close()


    def test_render_approved_from_page_passes_burn_subtitles_when_checked(self):
        job = self._create_job()
        self._create_candidate(job.id, status='approved', start_time=10.0, end_time=70.0, duration=60.0)
        with patch('app.web.pages.actions.render_candidate_clip') as mocked_render:
            mocked_render.return_value = (Mock(), 'C:/tmp/clip.ass', 'C:/tmp/clip.mp4')
            response = self.client.post(f'/jobs/{job.id}/view/render-approved', data={'mode': 'short', 'render_preset': 'impact', 'burn_subtitles': 'true'}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        mocked_render.assert_called_once()
        self.assertTrue(mocked_render.call_args.kwargs['burn_subtitles'])


    def test_bulk_render_from_page_passes_burn_subtitles_when_checked(self):
        job = self._create_job()
        candidate = self._create_candidate(job.id, status='pending', start_time=10.0, end_time=70.0, duration=60.0)
        with patch('app.web.pages.actions.render_candidate_clip') as mocked_render:
            mocked_render.return_value = (Mock(), 'C:/tmp/clip.ass', 'C:/tmp/clip.mp4')
            response = self.client.post(f'/jobs/{job.id}/view/candidates/bulk', data={'mode': 'short', 'bulk_action': 'render', 'candidate_ids': [candidate.id], 'render_preset': 'impact', 'burn_subtitles': 'true'}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        mocked_render.assert_called_once()
        self.assertTrue(mocked_render.call_args.kwargs['burn_subtitles'])


    def test_render_presets_endpoint_returns_available_presets(self):
        response = self.client.get('/jobs/render-presets')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['default'], 'clean')
        self.assertTrue(any((preset['key'] == 'impact' for preset in data['presets'])))


    def test_list_rendered_clips_returns_editorial_package(self):
        job = self._create_job()
        clip = self._create_clip(job.id)
        response = self.client.get(f'/jobs/{job.id}/clips')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['total_clips'], 1)
        self.assertEqual(data['clips'][0]['clip_id'], clip.id)
        self.assertEqual(data['clips'][0]['headline'], 'Titulo sugerido')
        self.assertEqual(data['clips'][0]['hashtags'], '#cortes #shorts')
        self.assertEqual(data['clips'][0]['suggested_filename'], 'clip-sugerido.mp4')
        self.assertEqual(data['clips'][0]['publication_status'], 'draft')
        self.assertEqual(data['clips'][0]['publication']['title'], 'Titulo sugerido')
        self.assertEqual(data['clips'][0]['publication']['hashtags'], ['#cortes', '#shorts'])
        self.assertEqual(data['clips'][0]['publication']['status_label'], 'Rascunho')
        self.assertIn('Descricao curta', data['clips'][0]['publication']['caption'])


    def test_job_detail_renders_queue_waiting_hint(self):
        job = self._create_job(status='pending', title='Esperando slot', error_message='Aguardando vaga na fila de processamento.')
        response = self.client.get(f'/jobs/{job.id}/view')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Na fila tecnica:', response.text)
        self.assertIn('slot pesado do pipeline', response.text)


    def test_job_detail_filters_candidates_and_exports(self):
        job = self._create_job(status='done', transcript_path='C:/tmp/transcript.json', detected_niche='podcast')
        self._create_candidate(job.id, status='approved', is_favorite=True, full_text='texto favorito')
        self._create_candidate(job.id, status='rejected', full_text='texto rejeitado')
        export_zip = self.test_artifacts_dir / f'job_{job.id}_export.zip'
        export_zip.write_bytes(b'fake zip')
        with patch('app.web.pages.job_detail.list_job_export_bundles', return_value=[{'name': export_zip.name, 'path': str(export_zip), 'size_bytes': export_zip.stat().st_size, 'modified_at': datetime.now(UTC)}]):
            response = self.client.get(f'/jobs/{job.id}/view', params={'candidate_filter': 'favorite', 'export_filter': 'latest'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('texto favorito', response.text)
        self.assertNotIn('texto rejeitado', response.text)
        self.assertIn(export_zip.name, response.text)


    def test_job_detail_renders_flash_feedback_banner(self):
        job = self._create_job(status='done', transcript_path='C:/tmp/transcript.json', detected_niche='podcast')
        self._create_candidate(job.id, full_text='texto existente')
        response = self.client.get(f'/jobs/{job.id}/view', params={'message': 'Atualizacao salva.', 'message_level': 'success'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Sucesso', response.text)
        self.assertIn('Atualizacao salva.', response.text)


    def test_job_detail_renders_manual_direct_card_without_transcript(self):
        job = self._create_job(status='extracting_audio', transcript_path=None, detected_niche=None)
        response = self.client.get(f'/jobs/{job.id}/view')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Render manual imediato', response.text)
        self.assertIn('Abrir render manual', response.text)
        self.assertIn('id="manual-render-card"', response.text)
        self.assertIn('Este corte manual pode sair agora.', response.text)


    def test_job_detail_enables_auto_refresh_for_active_job(self):
        job = self._create_job(status='llm_enrichment')
        response = self.client.get(f'/jobs/{job.id}/view')
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'const jobMonitorEndpoint = "/jobs/{job.id}/monitor";', response.text)
        self.assertIn('const jobAutoRefreshEnabled = true;', response.text)
        self.assertIn('const jobAutoRefreshIntervalMs = 4000;', response.text)
        self.assertIn('Monitoramento automatico ativo', response.text)
        self.assertIn('Pausar auto-refresh', response.text)
        self.assertIn('refreshJobMonitorPartial()', response.text)
        self.assertIn('id="overview-candidates-count"', response.text)
        self.assertIn('id="overview-clips-count"', response.text)
        self.assertIn('id="overview-exports-count"', response.text)
        self.assertIn('id="video-asset-card"', response.text)
        self.assertIn('syncAssetCard(', response.text)
        self.assertIn('syncOriginalVideoPreview(', response.text)


    def test_job_detail_renders_cancel_action_for_running_step(self):
        job = self._create_job(status='transcribing')
        db = self._session()
        try:
            db.add(JobStep(job_id=job.id, step_name='transcribing', status='running', attempts=1, details='{"progress_message": "Executando transcricao do audio"}'))
            db.commit()
        finally:
            db.close()
        response = self.client.get(f'/jobs/{job.id}/view')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Cancelar processamento', response.text)
        self.assertIn(f'/jobs/{job.id}/view/cancel', response.text)


    def test_job_detail_disables_auto_refresh_for_done_job(self):
        job = self._create_job(status='done')
        with patch('app.web.pages.helpers._ensure_page_candidates', return_value=[]):
            response = self.client.get(f'/jobs/{job.id}/view')
        self.assertEqual(response.status_code, 200)
        self.assertIn('const jobAutoRefreshEnabled = false;', response.text)
        self.assertNotIn('id="auto-refresh-toggle"', response.text)


    def test_job_detail_backfills_candidates_when_done_job_has_none(self):
        transcript_path = self.test_artifacts_dir / 'backfill_transcript.json'
        transcript_path.write_text('{"text": "texto completo"}', encoding='utf-8')
        job = self._create_job(status='done', transcript_path=str(transcript_path), detected_niche='podcast')
        db = self._session()
        try:
            db.query(Candidate).filter(Candidate.job_id == job.id).delete()
            db.commit()
        finally:
            db.close()
    
        class CandidateStub:
            id = 99
            mode = 'short'
            start_time = 10.0
            end_time = 70.0
            duration = 60.0
            heuristic_score = 9.1
            score = 9.3
            reason = 'gancho forte'
            opening_text = 'abertura'
            closing_text = 'fechamento'
            full_text = 'texto completo'
            hook_score = 2.0
            clarity_score = 1.5
            closure_score = 1.0
            emotion_score = 0.5
            duration_fit_score = 3.0
            transcript_context_score = 0.0
            llm_score = None
            llm_why = None
            llm_title = None
            llm_hook = None
            status = 'pending'
            is_favorite = False
            editorial_notes = None
    
        def regenerate_side_effect(db_session, job_row, mode='short'):
            return [CandidateStub()]
        with patch('app.web.pages.helpers.regenerate_candidates_for_job', side_effect=regenerate_side_effect) as mocked_regenerate:
            response = self.client.get(f'/jobs/{job.id}/view')
        self.assertEqual(response.status_code, 200)
        self.assertIn('texto completo', response.text)
        mocked_regenerate.assert_called_once()


    def test_job_detail_sorts_candidates_by_llm_score(self):
        job = self._create_job(status='done', transcript_path='C:/tmp/transcript.json', detected_niche='podcast')
        self._create_candidate(job.id, full_text='candidato heuristico mais forte', score=9.8, heuristic_score=9.8, llm_score=7.2)
        self._create_candidate(job.id, full_text='candidato mais forte para llm', score=8.9, heuristic_score=8.4, llm_score=9.6)
        response = self.client.get(f'/jobs/{job.id}/view', params={'candidate_sort': 'llm'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Confiança da LLM', response.text)
        self.assertLess(response.text.index('candidato mais forte para llm'), response.text.index('candidato heuristico mais forte'))


    def test_job_detail_filters_and_sorts_divergent_candidates(self):
        job = self._create_job(status='done', transcript_path='C:/tmp/transcript.json', detected_niche='podcast')
        self._create_candidate(job.id, full_text='candidato com divergencia forte', score=8.0, heuristic_score=9.5, llm_score=6.8)
        self._create_candidate(job.id, full_text='candidato alinhado', score=8.4, heuristic_score=8.3, llm_score=8.1)
        response = self.client.get(f'/jobs/{job.id}/view', params={'candidate_filter': 'divergent', 'candidate_sort': 'divergent'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Maior divergência', response.text)
        self.assertIn('divergência forte', response.text)
        self.assertIn('Heurístico gostou mais do corte do que a LLM', response.text)
        self.assertIn('Explicação adaptativa', response.text)
        self.assertIn('candidato com divergencia forte', response.text)
        self.assertNotIn('candidato alinhado', response.text)


    def test_update_clip_publication_status_from_page(self):
        job = self._create_job(status='done')
        clip = self._create_clip(job.id)
        response = self.client.post(f'/jobs/{job.id}/view/clips/{clip.id}/publication', data={'mode': 'short', 'render_preset': 'clean', 'status': 'published'}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        db = self._session()
        try:
            refreshed = db.query(Clip).filter(Clip.id == clip.id).one()
            self.assertEqual(refreshed.publication_status, 'published')
        finally:
            db.close()


    def test_job_detail_renders_publication_status_label(self):
        job = self._create_job(status='done')
        self._create_clip(job.id, publication_status='ready')
        response = self.client.get(f'/jobs/{job.id}/view')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Pronto', response.text)


    def test_retry_job_from_page_redirects_and_schedules_pipeline(self):
        job = self._create_job(status='failed')
        with patch('app.web.pages.actions.enqueue_pipeline_job') as mocked_enqueue:
            response = self.client.post(f'/jobs/{job.id}/view/retry', data={}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], f'/jobs/{job.id}/view')
        mocked_enqueue.assert_called_once()
        self.assertEqual(mocked_enqueue.call_args.args[1], job.id)
        self.assertEqual(mocked_enqueue.call_args.kwargs, {'force': False})
        db = self._session()
        try:
            refreshed_job = db.query(Job).filter(Job.id == job.id).one()
            self.assertEqual(refreshed_job.status, 'pending')
            self.assertIsNone(refreshed_job.error_message)
        finally:
            db.close()


    def test_retry_job_step_from_page_redirects_and_resets_downstream_state(self):
        job = self._create_job(status='failed', video_path='C:/tmp/video.mp4', audio_path='C:/tmp/audio.mp3', transcript_path='C:/tmp/transcript.json', detected_niche='podcast', niche_confidence='alta')
        db = self._session()
        try:
            db.add_all([JobStep(job_id=job.id, step_name='downloading', status='completed', attempts=1), JobStep(job_id=job.id, step_name='extracting_audio', status='completed', attempts=1), JobStep(job_id=job.id, step_name='transcribing', status='failed', attempts=2), JobStep(job_id=job.id, step_name='analyzing', status='completed', attempts=1)])
            db.commit()
        finally:
            db.close()
        with patch('app.web.pages.actions.enqueue_pipeline_job') as mocked_enqueue:
            response = self.client.post(f'/jobs/{job.id}/view/steps/transcribing/retry', data={}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], f'/jobs/{job.id}/view')
        mocked_enqueue.assert_called_once()
        self.assertEqual(mocked_enqueue.call_args.args[1], job.id)
        self.assertEqual(mocked_enqueue.call_args.kwargs, {'force': False, 'start_step': 'transcribing'})
        db = self._session()
        try:
            refreshed_job = db.query(Job).filter(Job.id == job.id).one()
            steps = {step.step_name: step for step in db.query(JobStep).filter(JobStep.job_id == job.id).all()}
            self.assertEqual(refreshed_job.status, 'pending')
            self.assertIsNone(refreshed_job.transcript_path)
            self.assertIsNone(refreshed_job.detected_niche)
            self.assertEqual(steps['transcribing'].status, 'pending')
            self.assertEqual(steps['analyzing'].status, 'pending')
        finally:
            db.close()


    def test_analyze_without_llm_from_page_completes_analysis_and_redirects(self):
        job = self._create_job(status='analyzing', transcript_path='C:/tmp/transcript.json', detected_niche=None, niche_confidence=None)
        with patch('app.web.pages.actions.complete_analysis_without_llm') as mocked_complete:
            response = self.client.post(f'/jobs/{job.id}/view/analyze-without-llm', data={'mode': 'short'}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn(f'/jobs/{job.id}/view', response.headers['location'])
        self.assertIn('Analise+concluida+sem+LLM.', response.headers['location'])
        mocked_complete.assert_called_once()


    def test_cancel_job_from_page_requests_cancellation_and_redirects(self):
        job = self._create_job(status='transcribing')
        db = self._session()
        try:
            db.add(JobStep(job_id=job.id, step_name='transcribing', status='running', attempts=1))
            db.commit()
        finally:
            db.close()
        response = self.client.post(f'/jobs/{job.id}/view/cancel', data={'mode': 'short'}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn('Cancelamento+solicitado', response.headers['location'])
        db = self._session()
        try:
            refreshed_job = db.query(Job).filter(Job.id == job.id).one()
            step = db.query(JobStep).filter(JobStep.job_id == job.id, JobStep.step_name == 'transcribing').one()
            self.assertEqual(refreshed_job.status, 'cancel_requested')
            self.assertEqual(refreshed_job.error_message, 'Cancelamento solicitado pelo usuario.')
            self.assertIn('"cancel_requested": true', step.details.lower())
        finally:
            db.close()


    def test_reset_job_step_from_page_redirects_and_zeros_attempts(self):
        job = self._create_job(status='failed', video_path='C:/tmp/video.mp4', audio_path='C:/tmp/audio.mp3', transcript_path='C:/tmp/transcript.json', detected_niche='podcast', niche_confidence='alta')
        db = self._session()
        try:
            db.add_all([JobStep(job_id=job.id, step_name='downloading', status='completed', attempts=1), JobStep(job_id=job.id, step_name='extracting_audio', status='completed', attempts=1), JobStep(job_id=job.id, step_name='transcribing', status='exhausted', attempts=MAX_STEP_ATTEMPTS), JobStep(job_id=job.id, step_name='analyzing', status='failed', attempts=2)])
            db.commit()
        finally:
            db.close()
        response = self.client.post(f'/jobs/{job.id}/view/steps/transcribing/reset', data={}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], f'/jobs/{job.id}/view')
        db = self._session()
        try:
            refreshed_job = db.query(Job).filter(Job.id == job.id).one()
            steps = {step.step_name: step for step in db.query(JobStep).filter(JobStep.job_id == job.id).all()}
            self.assertEqual(refreshed_job.status, 'pending')
            self.assertEqual(steps['transcribing'].attempts, 0)
            self.assertEqual(steps['analyzing'].attempts, 0)
            self.assertEqual(steps['transcribing'].status, 'pending')
            self.assertEqual(steps['analyzing'].status, 'pending')
        finally:
            db.close()


    def test_render_manual_creates_clip(self):
        job = self._create_job()
        with patch('app.services.render_workflow.generate_ass_for_clip', return_value='C:/tmp/clip.ass'), patch('app.services.render_workflow.render_clip', return_value='C:/tmp/clip_1.mp4'):
            response = self.client.post(f'/jobs/{job.id}/render-manual', json={'start': 12.0, 'end': 45.0, 'burn_subtitles': True, 'mode': 'short'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['job_id'], job.id)
        self.assertEqual(data['source'], 'manual')
        self.assertEqual(data['duration'], 33.0)
        self.assertTrue(data['subtitles_burned'])
        self.assertEqual(data['output_path'], 'C:/tmp/clip_1.mp4')
        self.assertIn('headline', data)
        self.assertIn('hashtags', data)
        self.assertIn('suggested_filename', data)
        db = self._session()
        try:
            clips = db.query(Clip).all()
            self.assertEqual(len(clips), 1)
            self.assertEqual(clips[0].job_id, job.id)
            self.assertEqual(clips[0].source, 'manual')
            self.assertEqual(clips[0].output_path, 'C:/tmp/clip_1.mp4')
        finally:
            db.close()


    def test_render_manual_accepts_hhmmss_timecodes(self):
        job = self._create_job()
        with patch('app.services.render_workflow.generate_ass_for_clip', return_value='C:/tmp/clip.ass'), patch('app.services.render_workflow.render_clip', return_value='C:/tmp/clip_timecode.mp4'):
            response = self.client.post(f'/jobs/{job.id}/render-manual', json={'start': '00:00:12', 'end': '00:01:45', 'burn_subtitles': False, 'mode': 'short'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['start'], 12.0)
        self.assertEqual(data['end'], 105.0)
        self.assertEqual(data['duration'], 93.0)


    def test_render_manual_from_page_accepts_hour_minute_second_fields(self):
        job = self._create_job()
        with patch('app.services.render_workflow.generate_ass_for_clip', return_value='C:/tmp/clip.ass'), patch('app.services.render_workflow.render_clip', return_value='C:/tmp/clip_page.mp4'):
            response = self.client.post(f'/jobs/{job.id}/view/render-manual', data={'start_hours': '0', 'start_minutes': '0', 'start_seconds': '30', 'end_hours': '0', 'end_minutes': '1', 'end_seconds': '20', 'mode': 'short', 'render_preset': 'clean'}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn(f'/jobs/{job.id}/view', response.headers['location'])
        db = self._session()
        try:
            clip = db.query(Clip).order_by(Clip.id.desc()).first()
            self.assertIsNotNone(clip)
            self.assertEqual(clip.start_time, 30.0)
            self.assertEqual(clip.end_time, 80.0)
            self.assertEqual(clip.duration, 50.0)
        finally:
            db.close()


    def test_render_manual_from_page_warns_when_subtitles_requested_without_transcript(self):
        job = self._create_job(transcript_path=None)
        with patch('app.services.render_workflow.render_clip', return_value='C:/tmp/clip_page_no_transcript.mp4'):
            response = self.client.post(f'/jobs/{job.id}/view/render-manual', data={'start_hours': '0', 'start_minutes': '0', 'start_seconds': '10', 'end_hours': '0', 'end_minutes': '0', 'end_seconds': '40', 'mode': 'short', 'render_preset': 'clean', 'burn_subtitles': 'true'}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Render concluido sem legenda embutida porque este job ainda nao possui transcricao.', response.text)


    def test_render_manual_rejects_invalid_time_range(self):
        job = self._create_job()
        response = self.client.post(f'/jobs/{job.id}/render-manual', json={'start': 45.0, 'end': 12.0, 'burn_subtitles': False, 'mode': 'short'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['detail'], 'end deve ser maior que start')


    def test_render_manual_without_transcript_still_creates_clip_without_subtitles(self):
        job = self._create_job(transcript_path=None)
        with patch('app.services.render_workflow.render_clip', return_value='C:/tmp/clip_manual_no_transcript.mp4'):
            response = self.client.post(f'/jobs/{job.id}/render-manual', json={'start': 12.0, 'end': 45.0, 'burn_subtitles': True, 'mode': 'short'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['subtitles_burned'])
        self.assertEqual(data['output_path'], 'C:/tmp/clip_manual_no_transcript.mp4')

