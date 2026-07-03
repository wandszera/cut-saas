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

class TestPipelineJobs(RoutesTestCase):
    def test_process_job_pipeline_persists_steps_and_skips_existing_artifacts(self):
        job = self._create_job(status='pending', video_path='C:/tmp/existing_video.mp4', audio_path='C:/tmp/existing_audio.mp3', transcript_path='C:/tmp/existing_transcript.json', detected_niche='podcast', niche_confidence='alta')
        with patch('app.services.pipeline.SessionLocal', self.TestingSessionLocal), patch('app.services.pipeline._path_exists', return_value=True), patch('app.services.pipeline.load_transcript', return_value={'text': 'texto existente'}), patch('app.services.pipeline.analyze_transcript_context', return_value={'main_topics': ['tema']}):
            process_job_pipeline(job.id)
        db = self._session()
        try:
            refreshed_job = db.query(Job).filter(Job.id == job.id).one()
            steps = db.query(JobStep).filter(JobStep.job_id == job.id).order_by(JobStep.id.asc()).all()
            self.assertEqual(refreshed_job.status, 'done')
            self.assertEqual([step.step_name for step in steps], ['downloading', 'extracting_audio', 'transcribing', 'analyzing', 'llm_enrichment'])
            self.assertTrue(all((step.status == 'skipped' for step in steps[:-1])))
            self.assertEqual(steps[-1].status, 'completed')
        finally:
            db.close()


    def test_process_job_pipeline_records_analysis_heartbeat_progress(self):
        job = self._create_job(status='pending', title='Video longo', video_path='C:/tmp/video.mp4', audio_path='C:/tmp/audio.mp3', transcript_path='C:/tmp/transcript.json')
        with patch('app.services.pipeline.SessionLocal', self.TestingSessionLocal), patch('app.services.pipeline._path_exists', return_value=True), patch('app.services.pipeline.load_transcript', return_value={'text': 'texto teste bem longo'}), patch('app.services.pipeline.detect_niche', return_value={'niche': 'podcast', 'confidence': 'alta'}), patch('app.services.pipeline.analyze_transcript_context', return_value={'main_topics': ['tema']}), patch('app.services.pipeline.ensure_default_candidates_for_job', return_value={'short': 2}):
            process_job_pipeline(job.id, start_from_step='analyzing')
        db = self._session()
        try:
            analyzing_step = db.query(JobStep).filter(JobStep.job_id == job.id, JobStep.step_name == 'analyzing').one()
            self.assertIn('"progress_message": "Gerando candidatos iniciais"', analyzing_step.details)
            self.assertIn('"heartbeat_at":', analyzing_step.details)
            self.assertIn('"progress_percent":', analyzing_step.details)
            llm_step = db.query(JobStep).filter(JobStep.job_id == job.id, JobStep.step_name == 'llm_enrichment').one()
            self.assertIn('"insights_generated": true', llm_step.details.lower())
        finally:
            db.close()


    def test_process_job_pipeline_records_analysis_progress_percent_during_candidate_generation(self):
        job = self._create_job(status='pending', title='Video longo', video_path='C:/tmp/video.mp4', audio_path='C:/tmp/audio.mp3', transcript_path='C:/tmp/transcript.json')
    
        def fake_candidates(db, job, *, modes=('short',), force=False, progress_callback=None):
            if progress_callback:
                progress_callback('Montando janelas candidatas', 66)
                progress_callback('Pontuando 120 candidato(s) iniciais', 76)
                progress_callback('Aplicando rerank e ordenacao final', 88)
                progress_callback('Persistindo candidatos aprovados (120/120)', 96)
            return {'short': 120}
        with patch('app.services.pipeline.SessionLocal', self.TestingSessionLocal), patch('app.services.pipeline._path_exists', return_value=True), patch('app.services.pipeline.load_transcript', return_value={'text': 'texto teste bem longo'}), patch('app.services.pipeline.detect_niche', return_value={'niche': 'podcast', 'confidence': 'alta'}), patch('app.services.pipeline.ensure_default_candidates_for_job', side_effect=fake_candidates), patch('app.services.pipeline.analyze_transcript_context', return_value={'main_topics': ['tema']}):
            process_job_pipeline(job.id, start_from_step='analyzing')
        db = self._session()
        try:
            analyzing_step = db.query(JobStep).filter(JobStep.job_id == job.id, JobStep.step_name == 'analyzing').one()
            self.assertIn('"progress_percent": 96', analyzing_step.details)
            self.assertIn('Persistindo candidatos aprovados', analyzing_step.details)
        finally:
            db.close()


    def test_process_job_pipeline_records_transcription_heartbeat_progress(self):
        job = self._create_job(status='pending', title='Video longo', video_path='C:/tmp/video.mp4', audio_path='C:/tmp/audio.mp3', transcript_path=None)
    
        def fake_transcribe(audio_path, job_id, progress_callback=None):
            if progress_callback:
                progress_callback('Carregando modelo Whisper (base)')
                progress_callback('Executando transcricao do audio')
                progress_callback('Salvando transcricao em JSON')
            return 'C:/tmp/generated_transcript.json'
        with patch('app.services.pipeline.SessionLocal', self.TestingSessionLocal), patch('app.services.pipeline._path_exists', side_effect=lambda value: value in {'C:/tmp/video.mp4', 'C:/tmp/audio.mp3'}), patch('app.services.pipeline.transcribe_audio', side_effect=fake_transcribe), patch('app.services.pipeline.load_transcript', return_value={'text': 'texto teste bem longo'}), patch('app.services.pipeline.detect_niche', return_value={'niche': 'podcast', 'confidence': 'alta'}), patch('app.services.pipeline.analyze_transcript_context', return_value={'main_topics': ['tema']}), patch('app.services.pipeline.ensure_default_candidates_for_job', return_value={'short': 2}):
            process_job_pipeline(job.id, start_from_step='transcribing')
        db = self._session()
        try:
            transcribing_step = db.query(JobStep).filter(JobStep.job_id == job.id, JobStep.step_name == 'transcribing').one()
            self.assertIn('"progress_message": "Salvando transcricao em JSON"', transcribing_step.details)
            self.assertIn('"heartbeat_at":', transcribing_step.details)
        finally:
            db.close()


    def test_process_job_pipeline_completes_when_llm_insights_fail(self):
        job = self._create_job(status='pending', title='Video longo', video_path='C:/tmp/video.mp4', audio_path='C:/tmp/audio.mp3', transcript_path='C:/tmp/transcript.json', detected_niche=None, niche_confidence=None)
        with patch('app.services.pipeline.SessionLocal', self.TestingSessionLocal), patch('app.services.pipeline._path_exists', return_value=True), patch('app.services.pipeline.load_transcript', return_value={'text': 'texto teste bem longo'}), patch('app.services.pipeline.detect_niche', return_value={'niche': 'podcast', 'confidence': 'alta'}), patch('app.services.pipeline.analyze_transcript_context', side_effect=RuntimeError('llm timeout')), patch('app.services.pipeline.ensure_default_candidates_for_job', return_value={'short': 2}):
            process_job_pipeline(job.id, start_from_step='analyzing')
        db = self._session()
        try:
            refreshed_job = db.query(Job).filter(Job.id == job.id).one()
            llm_step = db.query(JobStep).filter(JobStep.job_id == job.id, JobStep.step_name == 'llm_enrichment').one()
            self.assertEqual(refreshed_job.status, 'done')
            self.assertIn('"llm_insights_skipped": true', llm_step.details.lower())
            self.assertIn('"llm_insights_error": "llm timeout"', llm_step.details.lower())
        finally:
            db.close()


    def test_process_job_pipeline_opens_llm_circuit_breaker_after_repeated_failures(self):
        job = self._create_job(status='pending', title='Video longo', video_path='C:/tmp/video.mp4', audio_path='C:/tmp/audio.mp3', transcript_path='C:/tmp/transcript.json', detected_niche='podcast', niche_confidence='alta')
        db = self._session()
        try:
            db.add(JobStep(job_id=job.id, step_name='llm_enrichment', status='failed', attempts=2, error_message='llm timeout'))
            db.commit()
        finally:
            db.close()
        with patch('app.services.pipeline.SessionLocal', self.TestingSessionLocal), patch('app.services.pipeline._path_exists', return_value=True), patch('app.services.pipeline.load_transcript', return_value={'text': 'texto teste bem longo'}), patch('app.services.pipeline.analyze_transcript_context') as mocked_llm:
            process_job_pipeline(job.id, start_from_step='llm_enrichment')
        mocked_llm.assert_not_called()
        db = self._session()
        try:
            refreshed_job = db.query(Job).filter(Job.id == job.id).one()
            llm_step = db.query(JobStep).filter(JobStep.job_id == job.id, JobStep.step_name == 'llm_enrichment').one()
            self.assertEqual(refreshed_job.status, 'done')
            self.assertIn('"llm_circuit_breaker_opened": true', llm_step.details.lower())
            self.assertIn('"skip_llm_insights": true', llm_step.details.lower())
        finally:
            db.close()


    def test_process_job_pipeline_cancels_running_job(self):
        job = self._create_job(status='pending', title='Video longo', video_path='C:/tmp/video.mp4', audio_path='C:/tmp/audio.mp3', transcript_path=None)
        cancel_triggered = {'done': False}
    
        def fake_transcribe(audio_path, job_id, progress_callback=None):
            if progress_callback:
                progress_callback('Carregando modelo Whisper (base)')
            db = self._session()
            try:
                running_job = db.query(Job).filter(Job.id == job_id).one()
                running_job.status = 'cancel_requested'
                running_job.error_message = 'Cancelamento solicitado pelo usuario.'
                db.commit()
            finally:
                db.close()
            cancel_triggered['done'] = True
            if progress_callback:
                progress_callback('Executando transcricao do audio')
            return 'C:/tmp/should_not_finish.json'
        with patch('app.services.pipeline.SessionLocal', self.TestingSessionLocal), patch('app.services.pipeline._path_exists', side_effect=lambda value: value in {'C:/tmp/video.mp4', 'C:/tmp/audio.mp3'}), patch('app.services.pipeline.transcribe_audio', side_effect=fake_transcribe):
            process_job_pipeline(job.id, start_from_step='transcribing')
        self.assertTrue(cancel_triggered['done'])
        db = self._session()
        try:
            refreshed_job = db.query(Job).filter(Job.id == job.id).one()
            transcribing_step = db.query(JobStep).filter(JobStep.job_id == job.id, JobStep.step_name == 'transcribing').one()
            self.assertEqual(refreshed_job.status, 'canceled')
            self.assertEqual(refreshed_job.error_message, 'Processamento cancelado pelo usuario.')
            self.assertEqual(transcribing_step.status, 'failed')
            self.assertIn('Cancelado pelo usuario', transcribing_step.error_message)
        finally:
            db.close()


    def test_process_job_pipeline_queues_when_concurrency_limit_is_reached(self):
        active_job = self._create_job(status='transcribing', title='Ativo', video_path='C:/tmp/video_active.mp4', audio_path='C:/tmp/audio_active.mp3', transcript_path=None)
        queued_job = self._create_job(status='pending', title='Na fila', video_path='C:/tmp/video_pending.mp4', audio_path='C:/tmp/audio_pending.mp3', transcript_path=None)
        with patch('app.services.pipeline.SessionLocal', self.TestingSessionLocal), patch.object(__import__('app.services.pipeline', fromlist=['settings']).settings, 'max_concurrent_pipeline_jobs', 1):
            process_job_pipeline(queued_job.id, start_from_step='transcribing')
        db = self._session()
        try:
            refreshed_job = db.query(Job).filter(Job.id == queued_job.id).one()
            self.assertEqual(refreshed_job.status, 'pending')
            self.assertIn('Aguardando vaga na fila de processamento', refreshed_job.error_message)
            self.assertEqual(db.query(JobStep).filter(JobStep.job_id == queued_job.id, JobStep.step_name == 'transcribing').count(), 0)
            self.assertEqual(db.query(Job).filter(Job.id == active_job.id).one().status, 'transcribing')
        finally:
            db.close()


    def test_process_job_pipeline_drains_next_pending_job_after_completion(self):
        first_job = self._create_job(status='pending', title='Primeiro', video_path='C:/tmp/video_first.mp4', audio_path='C:/tmp/audio_first.mp3', transcript_path=None)
        second_job = self._create_job(status='pending', title='Segundo', video_path='C:/tmp/video_second.mp4', audio_path='C:/tmp/audio_second.mp3', transcript_path=None)
    
        def fake_transcribe(audio_path, job_id, progress_callback=None):
            if progress_callback:
                progress_callback('Executando transcricao do audio')
            return f'C:/tmp/transcript_{job_id}.json'
        with patch('app.services.pipeline.SessionLocal', self.TestingSessionLocal), patch.object(__import__('app.services.pipeline', fromlist=['settings']).settings, 'max_concurrent_pipeline_jobs', 1), patch('app.services.pipeline._path_exists', side_effect=lambda value: value in {'C:/tmp/video_first.mp4', 'C:/tmp/audio_first.mp3', 'C:/tmp/video_second.mp4', 'C:/tmp/audio_second.mp3', 'C:/tmp/transcript_1.json', 'C:/tmp/transcript_2.json'}), patch('app.services.pipeline.transcribe_audio', side_effect=fake_transcribe), patch('app.services.pipeline.load_transcript', return_value={'text': 'texto teste bem longo'}), patch('app.services.pipeline.detect_niche', return_value={'niche': 'podcast', 'confidence': 'alta'}), patch('app.services.pipeline.analyze_transcript_context', return_value={'main_topics': ['tema']}), patch('app.services.pipeline.ensure_default_candidates_for_job', return_value={'short': 1}):
            process_job_pipeline(first_job.id, start_from_step='transcribing')
        db = self._session()
        try:
            refreshed_first = db.query(Job).filter(Job.id == first_job.id).one()
            refreshed_second = db.query(Job).filter(Job.id == second_job.id).one()
            self.assertEqual(refreshed_first.status, 'done')
            self.assertEqual(refreshed_second.status, 'done')
            self.assertIsNone(refreshed_second.error_message)
        finally:
            db.close()


    def test_split_segments_into_time_chunks_keeps_overlap_for_large_transcript(self):
        segments = [{'start': 0.0, 'end': 300.0, 'text': 'a'}, {'start': 300.0, 'end': 600.0, 'text': 'b'}, {'start': 600.0, 'end': 900.0, 'text': 'c'}, {'start': 900.0, 'end': 1200.0, 'text': 'd'}]
        chunks = split_segments_into_time_chunks(segments, chunk_duration_seconds=700.0, overlap_seconds=120.0)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertEqual(chunks[0][0]['start'], 0.0)
        self.assertLessEqual(chunks[1][0]['start'], 600.0)
        self.assertGreaterEqual(chunks[1][0]['start'], 300.0)
        self.assertEqual(chunks[-1][-1]['end'], 1200.0)


    def test_candidate_limits_follow_settings_by_mode(self):
        with patch('app.services.candidates.settings') as mocked_settings:
            mocked_settings.short_min_candidates_per_job = 14
            mocked_settings.short_max_candidates_per_job = 55
            mocked_settings.long_min_candidates_per_job = 4
            mocked_settings.long_max_candidates_per_job = 18
            self.assertEqual(_get_mode_candidate_limits('short'), (14, 55))
            self.assertEqual(_get_mode_candidate_limits('long'), (4, 18))


    def test_trial_candidate_limits_cap_short_and_long_outputs(self):
        db = self._session()
        try:
            db.query(Subscription).delete()
            db.commit()
            self.assertEqual(_get_mode_candidate_limits('short', db=db, workspace_id=self.workspace_id), (0, 10))
            self.assertEqual(_get_mode_candidate_limits('long', db=db, workspace_id=self.workspace_id), (0, 3))
        finally:
            db.close()


    def test_process_job_pipeline_persists_candidates_incrementally_by_chunk(self):
        job = self._create_job(status='pending', title='Transcricao gigante', video_path='C:/tmp/video.mp4', audio_path='C:/tmp/audio.mp3', transcript_path='C:/tmp/transcript.json', detected_niche=None, niche_confidence=None)
        transcript_payload = {'text': 'texto longo ' * 200, 'segments': [{'start': 0.0, 'end': 120.0, 'text': 'abertura'}, {'start': 120.0, 'end': 240.0, 'text': 'contexto'}, {'start': 240.0, 'end': 360.0, 'text': 'gancho'}, {'start': 960.0, 'end': 1080.0, 'text': 'virada'}, {'start': 1080.0, 'end': 1200.0, 'text': 'fechamento'}, {'start': 1200.0, 'end': 1320.0, 'text': 'cta'}]}
    
        def fake_score_candidates(candidates, **kwargs):
            scored = []
            for index, candidate in enumerate(candidates, start=1):
                item = dict(candidate)
                item['score'] = 9.5 - index * 0.1
                item['base_score'] = item['score']
                item['reason'] = f'candidato {index}'
                scored.append(item)
            return scored[:1]
        with patch('app.services.pipeline.SessionLocal', self.TestingSessionLocal), patch('app.services.pipeline._path_exists', return_value=True), patch('app.services.pipeline.load_transcript', return_value={'text': transcript_payload['text']}), patch('app.services.candidates.load_segments', return_value=transcript_payload['segments']), patch('app.services.pipeline.detect_niche', return_value={'niche': 'podcast', 'confidence': 'alta'}), patch('app.services.pipeline.analyze_transcript_context', return_value={'main_topics': ['tema']}), patch('app.services.candidates.score_candidates', side_effect=fake_score_candidates):
            process_job_pipeline(job.id, start_from_step='analyzing')
        db = self._session()
        try:
            refreshed_job = db.query(Job).filter(Job.id == job.id).one()
            analyzing_step = db.query(JobStep).filter(JobStep.job_id == job.id, JobStep.step_name == 'analyzing').one()
            candidates = db.query(Candidate).filter(Candidate.job_id == job.id, Candidate.mode == 'short').order_by(Candidate.created_at.asc(), Candidate.id.asc()).all()
            self.assertEqual(refreshed_job.status, 'done')
            self.assertGreaterEqual(len(candidates), 2)
            self.assertIn('chunk', (analyzing_step.details or '').lower())
        finally:
            db.close()


    def test_cancel_running_job_releases_slot_and_starts_next_pending_job(self):
        running_job = self._create_job(status='transcribing', title='Cancelando agora', video_path='C:/tmp/video_running.mp4', audio_path='C:/tmp/audio_running.mp3', transcript_path=None)
        queued_job = self._create_job(status='pending', title='Proximo da fila', video_path='C:/tmp/video_queued.mp4', audio_path='C:/tmp/audio_queued.mp3', transcript_path=None)
        db = self._session()
        try:
            db.add(JobStep(job_id=running_job.id, step_name='transcribing', status='running', attempts=1))
            db.commit()
        finally:
            db.close()
    
        def fake_transcribe(audio_path, job_id, progress_callback=None):
            if progress_callback:
                progress_callback('Executando transcricao do audio')
            return f'C:/tmp/transcript_{job_id}.json'
        with patch('app.services.pipeline.SessionLocal', self.TestingSessionLocal), patch.object(__import__('app.services.pipeline', fromlist=['settings']).settings, 'max_concurrent_pipeline_jobs', 1), patch('app.services.pipeline._path_exists', side_effect=lambda value: value in {'C:/tmp/video_running.mp4', 'C:/tmp/audio_running.mp3', 'C:/tmp/video_queued.mp4', 'C:/tmp/audio_queued.mp3', 'C:/tmp/transcript_2.json'}), patch('app.services.pipeline.transcribe_audio', side_effect=fake_transcribe), patch('app.services.pipeline.load_transcript', return_value={'text': 'texto teste bem longo'}), patch('app.services.pipeline.detect_niche', return_value={'niche': 'podcast', 'confidence': 'alta'}), patch('app.services.pipeline.analyze_transcript_context', return_value={'main_topics': ['tema']}), patch('app.services.pipeline.ensure_default_candidates_for_job', return_value={'short': 1}):
            response = self.client.post(f'/jobs/{running_job.id}/cancel')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'cancel_requested')
        db = self._session()
        try:
            refreshed_running = db.query(Job).filter(Job.id == running_job.id).one()
            refreshed_queued = db.query(Job).filter(Job.id == queued_job.id).one()
            self.assertEqual(refreshed_running.status, 'cancel_requested')
            self.assertEqual(refreshed_queued.status, 'done')
            self.assertEqual(refreshed_queued.transcript_path, 'C:/tmp/transcript_2.json')
            self.assertIsNone(refreshed_queued.error_message)
        finally:
            db.close()


    def test_process_job_pipeline_persists_duration_and_attempt_metadata(self):
        job = self._create_job(status='pending', video_path='C:/tmp/existing_video.mp4', audio_path='C:/tmp/existing_audio.mp3', transcript_path=None, detected_niche=None, niche_confidence=None)
        with patch('app.services.pipeline.SessionLocal', self.TestingSessionLocal), patch('app.services.pipeline._path_exists', side_effect=lambda value: value in {'C:/tmp/existing_video.mp4', 'C:/tmp/existing_audio.mp3', 'C:/tmp/generated_transcript.json'}), patch('app.services.pipeline.transcribe_audio', return_value='C:/tmp/generated_transcript.json'), patch('app.services.pipeline.load_transcript', return_value={'text': 'texto teste'}), patch('app.services.pipeline.detect_niche', return_value={'niche': 'podcast', 'confidence': 'alta'}), patch('app.services.pipeline.analyze_transcript_context', return_value={'priority_keywords': ['resultado'], 'promising_ranges': []}), patch('app.services.pipeline.ensure_default_candidates_for_job', return_value={}):
            process_job_pipeline(job.id, start_from_step='transcribing')
        db = self._session()
        try:
            transcribing_step = db.query(JobStep).filter(JobStep.job_id == job.id, JobStep.step_name == 'transcribing').one()
            details = transcribing_step.details or ''
            self.assertIn('"attempt": 1', details)
            self.assertIn('"duration_seconds":', details)
            self.assertIn('"transcript_path": "C:/tmp/generated_transcript.json"', details)
            refreshed_job = db.query(Job).filter(Job.id == job.id).one()
            self.assertIn('priority_keywords', refreshed_job.transcript_insights)
            llm_step = db.query(JobStep).filter(JobStep.job_id == job.id, JobStep.step_name == 'llm_enrichment').one()
            self.assertEqual(llm_step.status, 'completed')
        finally:
            db.close()


    def test_process_job_pipeline_generates_default_short_candidates_after_analyzing(self):
        job = self._create_job(status='pending', video_path='C:/tmp/existing_video.mp4', audio_path='C:/tmp/existing_audio.mp3', transcript_path=None, detected_niche=None, niche_confidence=None)
        with patch('app.services.pipeline.SessionLocal', self.TestingSessionLocal), patch('app.services.pipeline._path_exists', return_value=True), patch('app.services.pipeline.transcribe_audio', return_value='C:/tmp/generated_transcript.json'), patch('app.services.pipeline.load_transcript', return_value={'text': 'texto teste'}), patch('app.services.pipeline.detect_niche', return_value={'niche': 'podcast', 'confidence': 'alta'}), patch('app.services.pipeline.ensure_default_candidates_for_job', return_value={'short': 1}) as mocked_generate:
            process_job_pipeline(job.id, start_from_step='transcribing')
        db = self._session()
        try:
            analyzing_step = db.query(JobStep).filter(JobStep.job_id == job.id, JobStep.step_name == 'analyzing').one()
            self.assertIn('"generated_candidates": {"short": 1}', analyzing_step.details)
            mocked_generate.assert_called_once()
        finally:
            db.close()


    def test_ensure_default_candidates_for_job_preserves_existing_candidates_without_force(self):
        job = self._create_job(status='done', transcript_path='C:/tmp/transcript.json', detected_niche='podcast')
        self._create_candidate(job.id, mode='short', status='approved', score=9.4)
        db = self._session()
        try:
            from app.services.candidates import ensure_default_candidates_for_job
            with patch('app.services.candidates.regenerate_candidates_for_job') as mocked_regenerate:
                summary = ensure_default_candidates_for_job(db, db.query(Job).filter(Job.id == job.id).one(), modes=('short',))
            self.assertEqual(summary['short'], 1)
            mocked_regenerate.assert_not_called()
        finally:
            db.close()


    def test_process_job_pipeline_retries_failed_step_and_preserves_attempt_count(self):
        job = self._create_job(status='pending', video_path='C:/tmp/existing_video.mp4', audio_path='C:/tmp/existing_audio.mp3', transcript_path=None, detected_niche=None, niche_confidence=None)
        with patch('app.services.pipeline.SessionLocal', self.TestingSessionLocal), patch('app.services.pipeline._path_exists', side_effect=lambda value: value in {'C:/tmp/existing_video.mp4', 'C:/tmp/existing_audio.mp3'}), patch('app.services.pipeline.transcribe_audio', side_effect=RuntimeError('falha temporária na transcrição')):
            process_job_pipeline(job.id)
        db = self._session()
        try:
            failed_job = db.query(Job).filter(Job.id == job.id).one()
            failed_step = db.query(JobStep).filter(JobStep.job_id == job.id, JobStep.step_name == 'transcribing').one()
            self.assertEqual(failed_job.status, 'failed')
            self.assertIn('falha temporária na transcrição', failed_job.error_message)
            self.assertEqual(failed_step.status, 'failed')
            self.assertEqual(failed_step.attempts, 1)
        finally:
            db.close()
        with patch('app.services.pipeline.SessionLocal', self.TestingSessionLocal), patch('app.services.pipeline._path_exists', side_effect=lambda value: value in {'C:/tmp/existing_video.mp4', 'C:/tmp/existing_audio.mp3'}), patch('app.services.pipeline.transcribe_audio', return_value='C:/tmp/recovered_transcript.json'), patch('app.services.pipeline.load_transcript', return_value={'text': 'texto recuperado'}), patch('app.services.pipeline.detect_niche', return_value={'niche': 'podcast', 'confidence': 'media'}):
            process_job_pipeline(job.id)
        db = self._session()
        try:
            recovered_job = db.query(Job).filter(Job.id == job.id).one()
            steps = db.query(JobStep).filter(JobStep.job_id == job.id).order_by(JobStep.id.asc()).all()
            steps_by_name = {step.step_name: step for step in steps}
            self.assertEqual(recovered_job.status, 'done')
            self.assertIsNone(recovered_job.error_message)
            self.assertEqual(recovered_job.transcript_path, 'C:/tmp/recovered_transcript.json')
            self.assertEqual(recovered_job.detected_niche, 'podcast')
            self.assertEqual(steps_by_name['downloading'].status, 'skipped')
            self.assertEqual(steps_by_name['extracting_audio'].status, 'skipped')
            self.assertEqual(steps_by_name['transcribing'].status, 'completed')
            self.assertEqual(steps_by_name['transcribing'].attempts, 2)
            self.assertEqual(steps_by_name['analyzing'].status, 'completed')
        finally:
            db.close()


    def test_process_job_pipeline_marks_step_exhausted_after_max_attempts(self):
        job = self._create_job(status='pending', video_path='C:/tmp/existing_video.mp4', audio_path='C:/tmp/existing_audio.mp3', transcript_path=None, detected_niche=None, niche_confidence=None)
        for _ in range(MAX_STEP_ATTEMPTS):
            with patch('app.services.pipeline.SessionLocal', self.TestingSessionLocal), patch('app.services.pipeline._path_exists', side_effect=lambda value: value in {'C:/tmp/existing_video.mp4', 'C:/tmp/existing_audio.mp3'}), patch('app.services.pipeline.transcribe_audio', side_effect=RuntimeError('falha persistente')):
                process_job_pipeline(job.id)
        response = self.client.get(f'/jobs/{job.id}')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data['can_retry'])
        self.assertTrue(data['can_force_retry'])
        self.assertTrue(data['has_exhausted_steps'])
        transcribing = next((step for step in data['steps'] if step['step_name'] == 'transcribing'))
        self.assertEqual(transcribing['status'], 'exhausted')
        self.assertTrue(transcribing['is_exhausted'])
        self.assertTrue(transcribing['can_force_retry'])
        self.assertEqual(transcribing['attempts'], MAX_STEP_ATTEMPTS)


    def test_process_job_pipeline_force_allows_retry_after_exhaustion(self):
        job = self._create_job(status='failed', video_path='C:/tmp/existing_video.mp4', audio_path='C:/tmp/existing_audio.mp3', transcript_path=None, detected_niche=None, niche_confidence=None)
        db = self._session()
        try:
            db.add(JobStep(job_id=job.id, step_name='transcribing', status='exhausted', attempts=MAX_STEP_ATTEMPTS, error_message='falha persistente'))
            db.commit()
        finally:
            db.close()
        with patch('app.services.pipeline.SessionLocal', self.TestingSessionLocal), patch('app.services.pipeline._path_exists', side_effect=lambda value: value in {'C:/tmp/existing_video.mp4', 'C:/tmp/existing_audio.mp3', 'C:/tmp/forced_recovery.json'}), patch('app.services.pipeline.transcribe_audio', return_value='C:/tmp/forced_recovery.json'), patch('app.services.pipeline.load_transcript', return_value={'text': 'texto recuperado com force'}), patch('app.services.pipeline.detect_niche', return_value={'niche': 'podcast', 'confidence': 'alta'}), patch('app.services.pipeline.ensure_default_candidates_for_job', return_value={'short': 1}), patch('app.services.pipeline.analyze_transcript_context', return_value={'main_topics': ['tema']}):
            process_job_pipeline(job.id, force=True)
        db = self._session()
        try:
            refreshed_job = db.query(Job).filter(Job.id == job.id).one()
            transcribing_step = db.query(JobStep).filter(JobStep.job_id == job.id, JobStep.step_name == 'transcribing').one()
            self.assertEqual(refreshed_job.status, 'done')
            self.assertEqual(transcribing_step.status, 'completed')
            self.assertEqual(transcribing_step.attempts, MAX_STEP_ATTEMPTS + 1)
        finally:
            db.close()

