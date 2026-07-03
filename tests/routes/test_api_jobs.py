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

class TestApiJobs(RoutesTestCase):
    def test_health_returns_ok(self):
        response = self.client.get('/health')
        if response.status_code != 200: print('BODY:', response.text); self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'message': 'ok'})


    def test_health_live_returns_ok(self):
        response = self.client.get('/health/live')
        if response.status_code != 200: print('BODY:', response.text); self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})


    def test_health_ready_returns_runtime_readiness_payload(self):
        with patch('app.main.build_runtime_readiness', return_value={'ready': True, 'checks_ok': 6, 'checks_total': 6, 'checks': [{'name': 'Banco', 'ok': True, 'status': 'ok', 'detail': 'db'}]}):
            response = self.client.get('/health/ready')
        if response.status_code != 200: print('BODY:', response.text); self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'ready')
        self.assertTrue(response.json()['ready'])
        self.assertEqual(response.json()['checks_ok'], 6)


    def test_create_youtube_job_success(self):
        with patch('app.api.jobs.core.fetch_youtube_metadata', return_value={'title': 'Titulo do video', 'video_id': 'abc123def45', 'duration_seconds': 1200}), patch('app.services.pipeline.download_youtube_media', return_value={'video_path': 'C:/tmp/job_1.mp4', 'title': 'Titulo do video', 'video_id': 'abc123def45'}), patch('app.services.pipeline.extract_audio_from_video', return_value='C:/tmp/job_1.mp3'), patch('app.services.pipeline.transcribe_audio', return_value='C:/tmp/job_1.json'), patch('app.services.pipeline.load_transcript', return_value={'text': 'mock', 'segments': []}):
            response = self.client.post('/jobs/youtube', json={'url': 'https://www.youtube.com/watch?v=abc123def45'})
        if response.status_code != 200: print('BODY:', response.text); self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'pending')
        db = self._session()
        try:
            db.expire_all()
            job = db.query(Job).one()
            if job.status != 'done': print('JOB ERROR:', getattr(job, 'error_message', None)); self.assertEqual(job.status, 'done')
            self.assertEqual(job.title, 'Titulo do video')
        finally:
            db.close()


    def test_create_local_video_job_success(self):
        from app.core.config import settings
        local_video = Path(settings.base_data_dir) / 'video_local.mp4'
        local_video.parent.mkdir(parents=True, exist_ok=True)
        local_video.write_bytes(b'fake-video')
        with patch('app.api.jobs.core.probe_video_duration_seconds', return_value=1200), \
             patch('app.api.jobs.core.extract_audio_from_video', return_value='C:/tmp/job_local.mp3'), \
             patch('app.api.jobs.core.transcribe_audio', return_value='C:/tmp/job_local.json'), \
             patch('app.services.pipeline.load_transcript', return_value={'text': 'mock', 'segments': []}):
            response = self.client.post('/jobs/local', json={'video_path': str(local_video), 'title': 'Video externo'})
        if response.status_code != 200: print('BODY:', response.text); self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['source_type'], 'local')
        self.assertEqual(data['status'], 'done')
        self.assertEqual(data['title'], 'Video externo')
        db = self._session()
        try:
            db.expire_all()
            job = db.query(Job).one()
            self.assertEqual(job.source_type, 'local')
            self.assertEqual(job.source_value, str(local_video.resolve()))
            self.assertEqual(job.video_path, str(local_video.resolve()))
        finally:
            db.close()


    def test_create_youtube_job_failure_marks_job_as_failed(self):
        with patch('app.api.jobs.core.fetch_youtube_metadata', return_value={'title': 'Titulo do video', 'video_id': 'abc123def45', 'duration_seconds': 1200}), patch('app.services.pipeline.download_youtube_media', side_effect=RuntimeError('falha simulada no download')):
            response = self.client.post('/jobs/youtube', json={'url': 'https://www.youtube.com/watch?v=abc123def45'})
        if response.status_code != 200: print('BODY:', response.text); self.assertEqual(response.status_code, 200)
        db = self._session()
        try:
            db.expire_all()
            job = db.query(Job).one()
            self.assertEqual(job.status, 'failed')
            self.assertIn('falha simulada no download', job.error_message)
        finally:
            db.close()


    def test_get_pipeline_health_returns_queue_and_duration_metrics(self):
        queued_job = self._create_job(status='pending', error_message='Aguardando vaga na fila de processamento.')
        self._create_job(status='transcribing', title='Ativo')
        self._create_job(status='failed', title='Falhou')
        self._create_job(status='canceled', title='Cancelado')
        db = self._session()
        try:
            db.add_all([JobStep(job_id=queued_job.id, step_name='transcribing', status='completed', attempts=1, details='{"duration_seconds": 12.5}'), JobStep(job_id=queued_job.id, step_name='analyzing', status='completed', attempts=1, details='{"duration_seconds": 30.0}'), JobStep(job_id=queued_job.id, step_name='llm_enrichment', status='running', attempts=1, details='{"heartbeat_at": "2026-04-18T18:00:00+00:00"}'), JobStep(job_id=queued_job.id, step_name='transcribing', status='completed', attempts=1, details='{"duration_seconds": 7.5}')])
            db.commit()
        finally:
            db.close()
        with patch('app.api.jobs.system.datetime') as mocked_datetime:
            mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
            mocked_datetime.now.return_value = datetime.fromisoformat('2026-04-18T19:00:01+00:00')
            mocked_datetime.utcnow.return_value = datetime(2026, 4, 18, 19, 0, 1)
            response = self.client.get('/jobs/health/pipeline')
        if response.status_code != 200: print('BODY:', response.text); self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['jobs']['queued'], 1)
        self.assertEqual(data['jobs']['active'], 1)
        self.assertEqual(data['jobs']['failed'], 1)
        self.assertEqual(data['jobs']['canceled'], 1)
        self.assertEqual(data['steps']['completed'], 3)
        self.assertEqual(data['steps']['stale_running'], 1)
        self.assertEqual(data['steps']['average_duration_seconds']['transcribing'], 10.0)
        self.assertEqual(data['steps']['average_duration_seconds']['analyzing'], 30.0)


    def test_get_dashboard_monitor_returns_compact_dashboard_payload(self):
        queued_job = self._create_job(status='pending', error_message='Aguardando vaga na fila de processamento.')
        active_job = self._create_job(status='transcribing', title='Ativo')
        done_job = self._create_job(status='done', title='Finalizado')
        self._create_clip(done_job.id)
        response = self.client.get('/jobs/dashboard/monitor')
        if response.status_code != 200: print('BODY:', response.text); self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['summary']['queued_jobs'], 1)
        self.assertEqual(data['summary']['active_jobs'], 1)
        self.assertEqual(data['summary']['jobs_with_clips'], 1)
        self.assertIn('pipeline_health', data)
        queued_payload = next((job for job in data['jobs'] if job['id'] == queued_job.id))
        active_payload = next((job for job in data['jobs'] if job['id'] == active_job.id))
        self.assertEqual(queued_payload['status'], 'pending')
        self.assertEqual(active_payload['status'], 'transcribing')
        self.assertIn('progress', active_payload)


    def test_retry_job_endpoint_requeues_failed_job(self):
        job = self._create_job(status='failed')
        with patch('app.api.jobs.core.enqueue_pipeline_job') as mocked_enqueue:
            response = self.client.post(f'/jobs/{job.id}/retry')
        if response.status_code != 200: print('BODY:', response.text); self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['job_id'], job.id)
        self.assertEqual(data['status'], 'pending')
        self.assertFalse(data['force'])
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


    def test_retry_job_endpoint_rejects_done_job(self):
        job = self._create_job(status='done')
        response = self.client.post(f'/jobs/{job.id}/retry')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['detail'], "Apenas jobs com status 'failed' ou 'pending' podem ser reprocessados")


    def test_retry_job_endpoint_blocks_exhausted_steps_without_force(self):
        job = self._create_job(status='failed')
        db = self._session()
        try:
            db.add(JobStep(job_id=job.id, step_name='transcribing', status='exhausted', attempts=MAX_STEP_ATTEMPTS, error_message='falha persistente'))
            db.commit()
        finally:
            db.close()
        response = self.client.post(f'/jobs/{job.id}/retry')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['detail'], 'Uma ou mais etapas excederam o limite de tentativas. Use force=true para tentar novamente.')


    def test_retry_job_endpoint_allows_force_for_exhausted_steps(self):
        job = self._create_job(status='failed')
        db = self._session()
        try:
            db.add(JobStep(job_id=job.id, step_name='transcribing', status='exhausted', attempts=MAX_STEP_ATTEMPTS, error_message='falha persistente'))
            db.commit()
        finally:
            db.close()
        with patch('app.api.jobs.core.enqueue_pipeline_job') as mocked_enqueue:
            response = self.client.post(f'/jobs/{job.id}/retry', params={'force': 'true'})
        if response.status_code != 200: print('BODY:', response.text); self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['force'])
        mocked_enqueue.assert_called_once()
        self.assertEqual(mocked_enqueue.call_args.args[1], job.id)
        self.assertEqual(mocked_enqueue.call_args.kwargs, {'force': True})


    def test_cancel_job_endpoint_requests_cancellation(self):
        job = self._create_job(status='transcribing')
        db = self._session()
        try:
            db.add(JobStep(job_id=job.id, step_name='transcribing', status='running', attempts=1))
            db.commit()
        finally:
            db.close()
        response = self.client.post(f'/jobs/{job.id}/cancel')
        if response.status_code != 200: print('BODY:', response.text); self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'cancel_requested')
        db = self._session()
        try:
            refreshed_job = db.query(Job).filter(Job.id == job.id).one()
            self.assertEqual(refreshed_job.status, 'cancel_requested')
            self.assertEqual(refreshed_job.error_message, 'Cancelamento solicitado pelo usuario.')
        finally:
            db.close()


    def test_cancel_job_endpoint_cancels_queued_job_immediately(self):
        job = self._create_job(status='pending', title='Na fila tecnica')
        response = self.client.post(f'/jobs/{job.id}/cancel')
        if response.status_code != 200: print('BODY:', response.text); self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'canceled')
        db = self._session()
        try:
            refreshed_job = db.query(Job).filter(Job.id == job.id).one()
            self.assertEqual(refreshed_job.status, 'canceled')
            self.assertEqual(refreshed_job.error_message, 'Processamento cancelado pelo usuario.')
        finally:
            db.close()


    def test_retry_job_step_endpoint_requeues_specific_step(self):
        job = self._create_job(status='failed', video_path='C:/tmp/video.mp4', audio_path='C:/tmp/audio.mp3', transcript_path='C:/tmp/transcript.json', detected_niche='podcast', niche_confidence='alta')
        db = self._session()
        try:
            db.add_all([JobStep(job_id=job.id, step_name='downloading', status='completed', attempts=1), JobStep(job_id=job.id, step_name='extracting_audio', status='completed', attempts=1), JobStep(job_id=job.id, step_name='transcribing', status='failed', attempts=1, error_message='erro'), JobStep(job_id=job.id, step_name='analyzing', status='completed', attempts=1)])
            db.commit()
        finally:
            db.close()
        with patch('app.api.jobs.core.enqueue_pipeline_job') as mocked_enqueue:
            response = self.client.post(f'/jobs/{job.id}/steps/transcribing/retry')
        if response.status_code != 200: print('BODY:', response.text); self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['step_name'], 'transcribing')
        self.assertFalse(data['force'])
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
            self.assertEqual(steps['downloading'].status, 'completed')
            self.assertEqual(steps['extracting_audio'].status, 'completed')
            self.assertEqual(steps['transcribing'].status, 'pending')
            self.assertEqual(steps['transcribing'].attempts, 1)
            self.assertEqual(steps['analyzing'].status, 'pending')
        finally:
            db.close()


    def test_retry_job_step_endpoint_blocks_exhausted_step_without_force(self):
        job = self._create_job(status='failed')
        db = self._session()
        try:
            db.add(JobStep(job_id=job.id, step_name='transcribing', status='exhausted', attempts=MAX_STEP_ATTEMPTS))
            db.commit()
        finally:
            db.close()
        response = self.client.post(f'/jobs/{job.id}/steps/transcribing/retry')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['detail'], "A etapa 'transcribing' excedeu o limite de tentativas. Use force=true para tentar novamente.")


    def test_reset_job_step_endpoint_resets_attempts_and_downstream_state(self):
        job = self._create_job(status='failed', video_path='C:/tmp/video.mp4', audio_path='C:/tmp/audio.mp3', transcript_path='C:/tmp/transcript.json', detected_niche='podcast', niche_confidence='alta')
        db = self._session()
        try:
            db.add_all([JobStep(job_id=job.id, step_name='downloading', status='completed', attempts=1), JobStep(job_id=job.id, step_name='extracting_audio', status='completed', attempts=1), JobStep(job_id=job.id, step_name='transcribing', status='exhausted', attempts=MAX_STEP_ATTEMPTS), JobStep(job_id=job.id, step_name='analyzing', status='failed', attempts=2)])
            db.commit()
        finally:
            db.close()
        response = self.client.post(f'/jobs/{job.id}/steps/transcribing/reset')
        if response.status_code != 200: print('BODY:', response.text); self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['step_name'], 'transcribing')
        self.assertTrue(data['reset_attempts'])
        db = self._session()
        try:
            refreshed_job = db.query(Job).filter(Job.id == job.id).one()
            steps = {step.step_name: step for step in db.query(JobStep).filter(JobStep.job_id == job.id).all()}
            self.assertEqual(refreshed_job.status, 'pending')
            self.assertIsNone(refreshed_job.transcript_path)
            self.assertIsNone(refreshed_job.detected_niche)
            self.assertEqual(steps['transcribing'].status, 'pending')
            self.assertEqual(steps['transcribing'].attempts, 0)
            self.assertEqual(steps['analyzing'].status, 'pending')
            self.assertEqual(steps['analyzing'].attempts, 0)
            self.assertEqual(steps['extracting_audio'].status, 'completed')
        finally:
            db.close()


    def test_get_job_returns_expected_payload(self):
        job = self._create_job(status='done', title='Job detalhado', video_path='C:/tmp/video.mp4', audio_path='C:/tmp/audio.mp3', transcript_path='C:/tmp/transcript.json')
        response = self.client.get(f'/jobs/{job.id}')
        if response.status_code != 200: print('BODY:', response.text); self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['id'], job.id)
        self.assertEqual(data['title'], 'Job detalhado')
        self.assertEqual(data['status'], 'done')
        self.assertEqual(data['video_path'], 'C:/tmp/video.mp4')
        self.assertEqual(data['audio_path'], 'C:/tmp/audio.mp3')
        self.assertEqual(data['transcript_path'], 'C:/tmp/transcript.json')
        self.assertIsNone(data['video_url'])
        self.assertIsNone(data['audio_url'])
        self.assertIsNone(data['transcript_url'])
        self.assertFalse(data['can_retry'])
        self.assertFalse(data['can_force_retry'])
        self.assertFalse(data['has_exhausted_steps'])
        self.assertEqual(data['max_step_attempts'], MAX_STEP_ATTEMPTS)
        self.assertEqual(data['steps'], [])


    def test_get_job_monitor_returns_compact_monitor_payload(self):
        job = self._create_job(status='llm_enrichment', title='Monitor job', video_path='C:/tmp/video.mp4', audio_path='C:/tmp/audio.mp3', transcript_path='C:/tmp/transcript.json')
        self._create_candidate(job.id, mode='short', score=9.2)
        self._create_clip(job.id, output_path='C:/tmp/clip.mp4', score=8.8)
        db = self._session()
        try:
            db.add(JobStep(job_id=job.id, step_name='llm_enrichment', status='running', attempts=1, details='{"progress_message": "Gerando insights da transcricao", "heartbeat_at": "2026-04-19T10:00:00+00:00"}'))
            db.commit()
        finally:
            db.close()
        with patch('app.api.jobs.core.list_job_export_bundles', return_value=[{'name': 'job_export.zip'}]), patch('app.api.jobs.core.build_static_url', side_effect=lambda path: f'/static/{Path(path).name}' if path else None):
            response = self.client.get(f'/jobs/{job.id}/monitor')
        if response.status_code != 200: print('BODY:', response.text); self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['id'], job.id)
        self.assertEqual(data['status'], 'llm_enrichment')
        self.assertEqual(data['video_path'], 'C:/tmp/video.mp4')
        self.assertEqual(data['audio_path'], 'C:/tmp/audio.mp3')
        self.assertEqual(data['transcript_path'], 'C:/tmp/transcript.json')
        self.assertTrue(data['video_url'])
        self.assertTrue(data['audio_url'])
        self.assertTrue(data['transcript_url'])
        self.assertEqual(data['overview']['candidates_count'], 1)
        self.assertEqual(data['overview']['clips_count'], 1)
        self.assertEqual(data['overview']['exports_count'], 1)
        self.assertIn('steps', data)
        self.assertEqual(len(data['steps']), 1)
        self.assertEqual(data['steps'][0]['step_name'], 'llm_enrichment')
        self.assertEqual(data['steps'][0]['progress_message'], 'Gerando insights da transcricao')


    def test_get_job_returns_404_for_missing_job(self):
        response = self.client.get('/jobs/9999')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['detail'], 'Job não encontrado')


    def test_get_job_includes_persisted_pipeline_steps(self):
        job = self._create_job()
        db = self._session()
        try:
            db.add(JobStep(job_id=job.id, step_name='downloading', status='completed', attempts=1, details='{"video_path":"C:/tmp/video.mp4"}'))
            db.commit()
        finally:
            db.close()
        response = self.client.get(f'/jobs/{job.id}')
        if response.status_code != 200: print('BODY:', response.text); self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data['steps']), 1)
        self.assertEqual(data['steps'][0]['step_name'], 'downloading')
        self.assertEqual(data['steps'][0]['status'], 'completed')
        self.assertFalse(data['steps'][0]['is_exhausted'])


    def test_get_job_returns_parsed_step_observability_fields(self):
        job = self._create_job(status='failed')
        db = self._session()
        try:
            db.add(JobStep(job_id=job.id, step_name='transcribing', status='failed', attempts=2, error_message='falha observada', details='{"attempt": 2, "duration_seconds": 1.234, "reason": "audio_missing", "audio_path": "C:/tmp/audio.mp3", "forced": true}'))
            db.commit()
        finally:
            db.close()
        response = self.client.get(f'/jobs/{job.id}')
        if response.status_code != 200: print('BODY:', response.text); self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data['steps']), 1)
        step = data['steps'][0]
        self.assertEqual(step['details_payload']['attempt'], 2)
        self.assertEqual(step['details_payload']['reason'], 'audio_missing')
        self.assertEqual(step['details_payload']['audio_path'], 'C:/tmp/audio.mp3')
        self.assertTrue(step['details_payload']['forced'])
        self.assertEqual(step['duration_seconds'], 1.234)
        self.assertEqual(step['duration_label'], '1.234s')
        self.assertIn('Motivo: audio_missing', step['summary_items'])
        self.assertIn('Tentativa registrada: 2', step['summary_items'])
        self.assertIn('Duração: 1.234s', step['summary_items'])
        self.assertIn('Execução forçada', step['summary_items'])


    def test_get_job_returns_running_step_progress_fields(self):
        job = self._create_job(status='analyzing')
        db = self._session()
        try:
            db.add(JobStep(job_id=job.id, step_name='analyzing', status='running', attempts=1, details='{"attempt": 1, "progress_message": "Gerando candidatos iniciais", "progress_percent": 52, "heartbeat_at": "2026-04-18T19:50:00+00:00"}'))
            db.commit()
        finally:
            db.close()
        response = self.client.get(f'/jobs/{job.id}')
        if response.status_code != 200: print('BODY:', response.text); self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data['steps']), 1)
        step = data['steps'][0]
        self.assertEqual(step['progress_message'], 'Gerando candidatos iniciais')
        self.assertEqual(step['progress_percent'], 52)
        self.assertEqual(step['heartbeat_at'], '2026-04-18T19:50:00+00:00')
        self.assertIn('Atividade: Gerando candidatos iniciais', step['summary_items'])
        self.assertIn('Ultima atividade: 2026-04-18T19:50:00+00:00', step['summary_items'])


    def test_api_lists_niches_with_summary_counts(self):
        self._create_niche_definition(name='Financas Creator', slug='financas-creator', status='pending')
        self._create_niche_definition(name='Historico', slug='historico', status='archived')
        response = self.client.get('/jobs/niches')
        if response.status_code != 200: print('BODY:', response.text); self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertGreaterEqual(data['active_count'], 1)
        self.assertEqual(data['pending_count'], 1)
        self.assertEqual(data['inactive_count'], 1)
        self.assertTrue(any((niche['slug'] == 'financas-creator' for niche in data['niches'])))


    def test_api_create_niche_returns_pending_payload(self):
        with patch('app.api.jobs.system.create_pending_niche', return_value={'name': 'Empreendedorismo Local', 'slug': 'empreendedorismo-local', 'description': 'Negocios locais, vendas e operacao.', 'keywords': ['vendas', 'caixa', 'cliente'], 'weights': {'hook': 1.1}, 'source': 'custom', 'status': 'pending', 'llm_notes': 'Sugestao consistente'}) as mocked_create:
            response = self.client.post('/jobs/niches', json={'name': 'Empreendedorismo Local', 'description': 'Pequenos negocios, vendas e caixa.'})
        if response.status_code != 200: print('BODY:', response.text); self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['niche']['slug'], 'empreendedorismo-local')
        self.assertEqual(data['niche']['status'], 'pending')
        mocked_create.assert_called_once()


    def test_api_approve_reject_and_archive_niche_endpoints(self):
        niche = self._create_niche_definition(name='Financas Creator', slug='financas-creator', status='pending')
        approve_response = self.client.post(f'/jobs/niches/{niche.slug}/approve')
        self.assertEqual(approve_response.status_code, 200)
        self.assertEqual(approve_response.json()['niche']['status'], 'active')
        reject_response = self.client.post(f'/jobs/niches/{niche.slug}/reject')
        self.assertEqual(reject_response.status_code, 200)
        self.assertEqual(reject_response.json()['niche']['status'], 'rejected')
        archive_response = self.client.post(f'/jobs/niches/{niche.slug}/archive')
        self.assertEqual(archive_response.status_code, 200)
        self.assertEqual(archive_response.json()['niche']['status'], 'archived')

