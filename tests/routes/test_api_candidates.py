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

class TestApiCandidates(RoutesTestCase):
    def test_analysis_calibration_endpoint_summarizes_real_editorial_history(self):
        job_a = self._create_job(detected_niche='podcast')
        job_b = self._create_job(detected_niche='podcast')
        self._create_candidate(job_a.id, mode='short', duration=82.0, opening_text='Por que esse erro derruba sua retenção?', status='approved', is_favorite=True)
        self._create_candidate(job_a.id, mode='short', duration=86.0, opening_text='Por que esse erro derruba sua retenção?', status='rendered')
        self._create_candidate(job_b.id, mode='short', duration=124.0, opening_text='Hoje eu vou falar sobre retenção', status='rejected')
        self._create_candidate(job_b.id, mode='short', duration=128.0, opening_text='Esse ponto aqui mostra tudo', status='rejected')
        response = self.client.get('/jobs/analysis-calibration', params={'mode': 'short', 'niche': 'podcast'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['calibration_active'])
        self.assertEqual(data['mode'], 'short')
        self.assertEqual(data['niche'], 'podcast')
        self.assertEqual(data['reviewed_count'], 4)
        self.assertLess(data['preferred_short_max_seconds'], 120.0)
        self.assertGreaterEqual(data['informative_opening_multiplier'], 1.2)
        self.assertGreaterEqual(data['context_penalty_multiplier'], 1.2)
        self.assertTrue(data['recommendations'])


    def test_candidate_status_endpoints_update_status(self):
        job = self._create_job()
        candidate = self._create_candidate(job.id)
        approve_response = self.client.post(f'/jobs/candidates/{candidate.id}/approve')
        self.assertEqual(approve_response.status_code, 200)
        self.assertEqual(approve_response.json()['status'], 'approved')
        reject_response = self.client.post(f'/jobs/candidates/{candidate.id}/reject')
        self.assertEqual(reject_response.status_code, 200)
        self.assertEqual(reject_response.json()['status'], 'rejected')
        reset_response = self.client.post(f'/jobs/candidates/{candidate.id}/reset')
        self.assertEqual(reset_response.status_code, 200)
        self.assertEqual(reset_response.json()['status'], 'pending')
        db = self._session()
        try:
            refreshed = db.query(Candidate).filter(Candidate.id == candidate.id).one()
            self.assertEqual(refreshed.status, 'pending')
        finally:
            db.close()


    def test_list_approved_candidates_returns_only_approved_for_mode(self):
        job = self._create_job()
        approved = self._create_candidate(job.id, status='approved', score=9.5, transcript_context_score=1.1, llm_score=8.2, llm_why='bom equilíbrio entre gancho e clareza', llm_title='Título aprovado pela LLM', llm_hook='Gancho aprovado pela LLM')
        self._create_candidate(job.id, status='pending', score=8.0)
        self._create_candidate(job.id, status='approved', mode='long', score=9.9)
        response = self.client.get(f'/jobs/{job.id}/approved-candidates', params={'mode': 'short'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['total_approved_candidates'], 1)
        self.assertEqual(data['candidates'][0]['candidate_id'], approved.id)
        self.assertEqual(data['candidates'][0]['status'], 'approved')
        self.assertEqual(data['candidates'][0]['transcript_context_score'], 1.1)
        self.assertEqual(data['candidates'][0]['llm_score'], 8.2)
        self.assertEqual(data['candidates'][0]['llm_title'], 'Título aprovado pela LLM')
        self.assertIn('adaptive_blend_explanation', data['candidates'][0])


    def test_render_candidate_by_id_creates_clip_and_marks_candidate_rendered(self):
        job = self._create_job()
        candidate = self._create_candidate(job.id, status='approved')
        with patch('app.services.render_workflow.render_clip', return_value='C:/tmp/candidate_clip.mp4'):
            response = self.client.post(f'/jobs/{job.id}/render-candidate-id/{candidate.id}')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['candidate_id'], candidate.id)
        self.assertEqual(data['output_path'], 'C:/tmp/candidate_clip.mp4')
        self.assertIn('headline', data)
        self.assertIn('hashtags', data)
        db = self._session()
        try:
            refreshed_candidate = db.query(Candidate).filter(Candidate.id == candidate.id).one()
            clips = db.query(Clip).all()
            self.assertEqual(refreshed_candidate.status, 'rendered')
            self.assertEqual(len(clips), 1)
            self.assertEqual(clips[0].source, 'candidate')
            self.assertEqual(clips[0].output_path, 'C:/tmp/candidate_clip.mp4')
        finally:
            db.close()


    def test_render_candidate_ranked_creates_clip_from_selected_index(self):
        job = self._create_job()
        ranked_candidates = [{'start': 15.0, 'end': 75.0, 'duration': 60.0, 'score': 9.4, 'reason': 'gancho forte', 'text': 'texto do primeiro candidato'}, {'start': 90.0, 'end': 150.0, 'duration': 60.0, 'score': 8.7, 'reason': 'bom fechamento', 'text': 'texto do segundo candidato'}]
        with patch('app.api.jobs.candidates._get_ranked_candidates', return_value=ranked_candidates), patch('app.services.render_workflow.generate_ass_for_clip', return_value='C:/tmp/ranked.ass'), patch('app.services.render_workflow.render_clip', return_value='C:/tmp/ranked_clip.mp4'):
            response = self.client.post(f'/jobs/{job.id}/render-candidate', json={'candidate_index': 1, 'burn_subtitles': True, 'mode': 'short'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['candidate_index'], 1)
        self.assertEqual(data['start'], 90.0)
        self.assertEqual(data['end'], 150.0)
        self.assertEqual(data['output_path'], 'C:/tmp/ranked_clip.mp4')
        self.assertTrue(data['subtitles_burned'])
        self.assertIn('headline', data)
        self.assertIn('suggested_filename', data)
        db = self._session()
        try:
            clips = db.query(Clip).all()
            self.assertEqual(len(clips), 1)
            self.assertEqual(clips[0].source, 'candidate')
            self.assertEqual(clips[0].start_time, 90.0)
        finally:
            db.close()


    def test_render_candidate_ranked_rejects_invalid_index(self):
        job = self._create_job()
        ranked_candidates = [{'start': 15.0, 'end': 75.0, 'duration': 60.0, 'score': 9.4, 'reason': 'gancho forte', 'text': 'texto do primeiro candidato'}]
        with patch('app.api.jobs.candidates._get_ranked_candidates', return_value=ranked_candidates):
            response = self.client.post(f'/jobs/{job.id}/render-candidate', json={'candidate_index': 4, 'burn_subtitles': False, 'mode': 'short'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('candidate_index inválido', response.json()['detail'])


    def test_render_top_clips_returns_ranked_rendered_payload(self):
        job = self._create_job()
        ranked_candidates = [{'start': 10.0, 'end': 70.0, 'duration': 60.0, 'score': 9.8, 'reason': 'abertura muito forte', 'text': 'texto 1'}, {'start': 90.0, 'end': 150.0, 'duration': 60.0, 'score': 8.9, 'reason': 'boa retenção', 'text': 'texto 2'}]
    
        def render_side_effect(**kwargs):
            return f"C:/tmp/top_clip_{kwargs['clip_index']}.mp4"
        with patch('app.api.jobs.candidates._get_ranked_candidates', return_value=ranked_candidates), patch('app.services.render_workflow.render_clip', side_effect=render_side_effect):
            response = self.client.post(f'/jobs/{job.id}/render', json={'top_n': 2, 'burn_subtitles': False, 'mode': 'short'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['rendered_clips_count'], 2)
        self.assertEqual(data['format'], '9:16')
        self.assertEqual(data['clips'][0]['output_path'], 'C:/tmp/top_clip_0.mp4')
        self.assertEqual(data['clips'][1]['output_path'], 'C:/tmp/top_clip_1.mp4')


    def test_render_top_clips_rejects_invalid_mode(self):
        job = self._create_job()
        response = self.client.post(f'/jobs/{job.id}/render', json={'top_n': 1, 'burn_subtitles': False, 'mode': 'invalid'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['detail'], "mode deve ser 'short' ou 'long'")


    def test_render_approved_candidates_renders_all_approved_and_updates_status(self):
        job = self._create_job()
        first = self._create_candidate(job.id, status='approved', score=9.8, start_time=10.0, end_time=70.0, duration=60.0)
        second = self._create_candidate(job.id, status='approved', score=8.9, start_time=90.0, end_time=150.0, duration=60.0)
        self._create_candidate(job.id, status='pending', score=9.7, start_time=160.0, end_time=220.0, duration=60.0)
    
        def render_side_effect(**kwargs):
            return f"C:/tmp/rendered_{kwargs['clip_index']}.mp4"
        with patch('app.services.render_workflow.render_clip', side_effect=render_side_effect):
            response = self.client.post(f'/jobs/{job.id}/render-approved', params={'mode': 'short', 'burn_subtitles': 'false'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['rendered_count'], 2)
        self.assertEqual({clip['candidate_id'] for clip in data['clips']}, {first.id, second.id})
        db = self._session()
        try:
            candidates = db.query(Candidate).order_by(Candidate.id.asc()).all()
            clips = db.query(Clip).order_by(Clip.id.asc()).all()
            statuses = {candidate.id: candidate.status for candidate in candidates}
            self.assertEqual(statuses[first.id], 'rendered')
            self.assertEqual(statuses[second.id], 'rendered')
            pending_candidate = next((candidate for candidate in candidates if candidate.id not in {first.id, second.id}))
            self.assertEqual(pending_candidate.status, 'pending')
            self.assertEqual(len(clips), 2)
        finally:
            db.close()


    def test_ranking_insights_returns_hybrid_weights_divergence_and_distribution(self):
        job = self._create_job(status='done', detected_niche='podcast')
        reference_job = self._create_job(status='done', detected_niche='podcast')
        self._create_candidate(reference_job.id, mode='short', status='approved', score=9.4, heuristic_score=7.2, llm_score=9.5, duration=55.0, full_text='erro de margem com exemplo pratico')
        self._create_candidate(reference_job.id, mode='short', status='rejected', score=7.1, heuristic_score=8.8, llm_score=6.0, duration=95.0, full_text='explicacao generica sem foco')
        candidate_a = self._create_candidate(job.id, mode='short', status='pending', is_favorite=True, score=9.6, heuristic_score=7.1, llm_score=9.6, duration=58.0, full_text='erro de margem com gancho forte e exemplo claro')
        candidate_b = self._create_candidate(job.id, mode='short', status='approved', score=8.2, heuristic_score=8.7, llm_score=7.0, duration=92.0, full_text='passo a passo com contexto bom')
        self._create_candidate(job.id, mode='short', status='rendered', score=6.8, heuristic_score=6.8, llm_score=None, duration=28.0, full_text='trecho curto complementar')
        response = self.client.get(f'/jobs/{job.id}/ranking-insights?mode=short')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['job_id'], job.id)
        self.assertEqual(data['mode'], 'short')
        self.assertEqual(data['candidate_summary']['total_candidates'], 3)
        self.assertEqual(data['candidate_summary']['llm_scored_count'], 2)
        self.assertEqual(data['candidate_summary']['favorite_count'], 1)
        self.assertEqual(data['candidate_summary']['status_counts']['pending'], 1)
        self.assertEqual(data['candidate_summary']['status_counts']['approved'], 1)
        self.assertEqual(data['candidate_summary']['status_counts']['rendered'], 1)
        self.assertEqual(data['divergence_summary']['moderate_or_higher_count'], 2)
        self.assertEqual(data['divergence_summary']['strong_count'], 1)
        self.assertEqual(data['divergence_summary']['llm_favored_count'], 1)
        self.assertEqual(data['divergence_summary']['heuristic_favored_count'], 1)
        self.assertEqual(data['divergence_summary']['top_divergent_candidates'][0]['candidate_id'], candidate_a.id)
        self.assertEqual(data['divergence_summary']['top_divergent_candidates'][1]['candidate_id'], candidate_b.id)
        self.assertEqual(data['weights']['preferred_source'], 'balanced')
        self.assertEqual(data['weights']['heuristic_weight'], 0.6)
        self.assertEqual(data['weights']['llm_weight'], 0.4)
        self.assertEqual(data['weights']['reviewed_count'], 3)
        self.assertEqual(data['distribution']['final_score']['count'], 3)
        self.assertEqual(data['distribution']['final_score']['buckets'][0]['count'], 1)
        self.assertEqual(data['distribution']['final_score']['buckets'][1]['count'], 1)
        self.assertEqual(data['distribution']['final_score']['buckets'][3]['count'], 1)
        self.assertEqual(data['distribution']['duration_seconds']['buckets'][0]['count'], 1)
        self.assertEqual(data['distribution']['duration_seconds']['buckets'][1]['count'], 1)
        self.assertEqual(data['distribution']['duration_seconds']['buckets'][2]['count'], 0)
        self.assertEqual(data['distribution']['duration_seconds']['buckets'][3]['count'], 1)


    def test_ranking_insights_handles_jobs_without_candidates(self):
        job = self._create_job(status='done', detected_niche='podcast')
        response = self.client.get(f'/jobs/{job.id}/ranking-insights?mode=short')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['candidate_summary']['total_candidates'], 0)
        self.assertEqual(data['candidate_summary']['llm_scored_count'], 0)
        self.assertEqual(data['divergence_summary']['compared_candidates'], 0)
        self.assertIsNone(data['distribution']['final_score']['avg'])
        self.assertEqual(data['distribution']['final_score']['buckets'][0]['count'], 0)
        self.assertEqual(data['distribution']['duration_seconds']['buckets'][3]['count'], 0)


    def test_list_candidates_returns_only_requested_mode_sorted_by_score(self):
        job = self._create_job()
        high = self._create_candidate(job.id, mode='short', score=9.8, start_time=10.0, end_time=70.0)
        low = self._create_candidate(job.id, mode='short', score=8.1, start_time=80.0, end_time=140.0)
        self._create_candidate(job.id, mode='long', score=9.9, start_time=150.0, end_time=450.0, duration=300.0)
        response = self.client.get(f'/jobs/{job.id}/candidates', params={'mode': 'short'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['job_id'], job.id)
        self.assertEqual(data['mode'], 'short')
        self.assertEqual(data['total_candidates'], 2)
        self.assertEqual([row['candidate_id'] for row in data['candidates']], [high.id, low.id])


    def test_list_candidates_rejects_invalid_mode(self):
        job = self._create_job()
        response = self.client.get(f'/jobs/{job.id}/candidates', params={'mode': 'invalid'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['detail'], "mode deve ser 'short' ou 'long'")


    def test_analyze_job_returns_ranked_candidates(self):
        job = self._create_job()
    
        class CandidateStub:
    
            def __init__(self, candidate_id, start, end, score):
                self.id = candidate_id
                self.start_time = start
                self.end_time = end
                self.duration = round(end - start, 2)
                self.heuristic_score = score + 0.3
                self.score = score
                self.reason = 'gancho forte'
                self.opening_text = 'abertura'
                self.closing_text = 'fechamento'
                self.full_text = 'texto completo'
                self.hook_score = 2.0
                self.clarity_score = 1.5
                self.closure_score = 1.0
                self.emotion_score = 0.5
                self.duration_fit_score = 3.0
                self.transcript_context_score = 1.4 if candidate_id == 1 else -0.6
                self.llm_score = 8.8 if candidate_id == 1 else None
                self.llm_why = 'tem começo forte e funciona sozinho' if candidate_id == 1 else None
                self.llm_title = 'Título editorial' if candidate_id == 1 else None
                self.llm_hook = 'Gancho editorial' if candidate_id == 1 else None
                self.status = 'pending'
        saved_candidates = [CandidateStub(1, 10.0, 70.0, 9.2), CandidateStub(2, 90.0, 150.0, 8.4)]
        with patch('app.api.jobs.candidates.regenerate_candidates_for_job', return_value=saved_candidates):
            response = self.client.post(f'/jobs/{job.id}/analyze', json={'mode': 'short', 'top_n': 1})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['job_id'], job.id)
        self.assertEqual(data['mode'], 'short')
        self.assertEqual(data['total_candidates'], 2)
        self.assertEqual(len(data['segments']), 1)
        self.assertEqual(data['segments'][0]['candidate_id'], 1)
        self.assertEqual(data['segments'][0]['score'], 9.2)
        self.assertEqual(data['segments'][0]['transcript_context_score'], 1.4)
        self.assertEqual(data['segments'][0]['llm_score'], 8.8)
        self.assertEqual(data['segments'][0]['llm_title'], 'Título editorial')
        self.assertIn('adaptive_blend_explanation', data['segments'][0])


    def test_get_job_feedback_profile_returns_learning_summary(self):
        target_job = self._create_job(status='done', detected_niche='podcast')
        reference_job = self._create_job(status='done', detected_niche='podcast')
        self._create_candidate(reference_job.id, status='approved', mode='short', full_text='resultado prÃ¡tico com exemplo forte', hook_score=3.5, clarity_score=2.4, closure_score=2.1, emotion_score=1.2, duration_fit_score=4.3)
        self._create_candidate(reference_job.id, status='rendered', mode='short', full_text='resultado claro com exemplo real', hook_score=3.2, clarity_score=2.2, closure_score=2.0, emotion_score=1.1, duration_fit_score=4.0)
        self._create_candidate(reference_job.id, status='rejected', mode='short', full_text='fala vaga e repetitiva sem exemplo', hook_score=0.8, clarity_score=0.5, closure_score=0.4, emotion_score=0.2, duration_fit_score=1.0)
        response = self.client.get(f'/jobs/{target_job.id}/feedback-profile', params={'mode': 'short'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['job_id'], target_job.id)
        self.assertEqual(data['niche'], 'podcast')
        self.assertEqual(data['mode'], 'short')
        self.assertTrue(data['feedback_profile']['min_samples_reached'])
        self.assertEqual(data['feedback_profile']['positive_count'], 2)
        self.assertEqual(data['feedback_profile']['negative_count'], 1)
        self.assertIn('resultado', data['feedback_profile']['successful_keywords'])
        self.assertIn('hybrid_weight_profile', data['feedback_profile'])
        self.assertIn('heuristic_weight', data['feedback_profile']['hybrid_weight_profile'])


    def test_recalibrate_job_feedback_profile_returns_updated_summary(self):
        target_job = self._create_job(status='done', detected_niche='podcast')
        with patch('app.api.jobs.feedback.learn_keywords_for_niche', return_value=[object(), object()]) as mocked_learn, patch('app.api.jobs.feedback.get_feedback_profile_for_niche', return_value={'niche': 'podcast', 'mode': 'short', 'positive_count': 3, 'negative_count': 1, 'sample_count': 4, 'min_samples_reached': True, 'successful_keywords': ['resultado', 'exemplo'], 'positive_means': {'hook_score': 3.1}, 'negative_means': {'hook_score': 0.7}, 'hybrid_weight_profile': {'reviewed_count': 3, 'approved_count': 2, 'rejected_count': 1, 'preferred_source': 'heuristic', 'heuristic_weight': 0.7, 'llm_weight': 0.3}}) as mocked_profile:
            response = self.client.post(f'/jobs/{target_job.id}/feedback-profile/recalibrate', params={'mode': 'short'})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['job_id'], target_job.id)
        self.assertEqual(data['learned_keywords_count'], 2)
        self.assertTrue(data['feedback_profile']['min_samples_reached'])
        self.assertEqual(data['feedback_profile']['successful_keywords'], ['resultado', 'exemplo'])
        self.assertEqual(data['feedback_profile']['hybrid_weight_profile']['heuristic_weight'], 0.7)
        mocked_learn.assert_called_once()
        mocked_profile.assert_called_once()


    def test_list_clips_returns_rendered_clips_sorted_by_created_at_desc(self):
        job = self._create_job()
        first = self._create_clip(job.id, output_path='C:/tmp/clip_a.mp4', score=7.5)
        second = self._create_clip(job.id, output_path='C:/tmp/clip_b.mp4', score=9.1)
        db = self._session()
        try:
            older = datetime.now(UTC) - timedelta(minutes=5)
            newer = datetime.now(UTC)
            db.query(Clip).filter(Clip.id == first.id).update({'created_at': older})
            db.query(Clip).filter(Clip.id == second.id).update({'created_at': newer})
            db.commit()
        finally:
            db.close()
        response = self.client.get(f'/jobs/{job.id}/clips')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['job_id'], job.id)
        self.assertEqual(data['total_clips'], 2)
        self.assertEqual(data['clips'][0]['clip_id'], second.id)
        self.assertEqual(data['clips'][1]['clip_id'], first.id)
        self.assertEqual(data['clips'][0]['output_path'], 'C:/tmp/clip_b.mp4')
        self.assertIsNone(data['clips'][0]['output_url'])


    def test_update_clip_publication_status_endpoint(self):
        job = self._create_job()
        clip = self._create_clip(job.id)
        response = self.client.post(f'/jobs/clips/{clip.id}/publication', params={'status': 'ready'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['publication_status'], 'ready')
        self.assertEqual(response.json()['publication_status_label'], 'Pronto')
        self.assertEqual(response.json()['publication']['status_label'], 'Pronto')
        db = self._session()
        try:
            refreshed = db.query(Clip).filter(Clip.id == clip.id).one()
            self.assertEqual(refreshed.publication_status, 'ready')
        finally:
            db.close()


    def test_update_clip_publication_status_rejects_invalid_status(self):
        job = self._create_job()
        clip = self._create_clip(job.id)
        response = self.client.post(f'/jobs/clips/{clip.id}/publication', params={'status': 'unknown'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['detail'], 'Status de publicacao invalido')


    def test_export_job_bundle_returns_zip_response(self):
        job = self._create_job()
        self._create_clip(job.id)
        export_zip = self.test_artifacts_dir / 'job_1_export.zip'
        export_zip.write_bytes(b'fake zip')
        with patch('app.api.jobs.clips.build_job_export_bundle', return_value=str(export_zip)):
            response = self.client.get(f'/jobs/{job.id}/export')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['content-type'], 'application/zip')
        self.assertIn('job_1_export.zip', response.headers['content-disposition'])


    def test_list_job_exports_returns_history(self):
        job = self._create_job()
        export_zip = self.test_artifacts_dir / f'job_{job.id}_export.zip'
        export_zip.write_bytes(b'fake zip')
        with patch('app.api.jobs.clips.list_job_export_bundles', return_value=[{'name': export_zip.name, 'path': str(export_zip), 'size_bytes': export_zip.stat().st_size, 'created_at': datetime(2026, 4, 24, 12, 30, tzinfo=UTC), 'modified_at': datetime.now(UTC)}]):
            response = self.client.get(f'/jobs/{job.id}/exports')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['total_exports'], 1)
        self.assertEqual(data['exports'][0]['name'], export_zip.name)
        self.assertEqual(data['exports'][0]['created_at'], '2026-04-24T12:30:00+00:00')
        self.assertIn(f'/jobs/{job.id}/export/files/', data['exports'][0]['download_url'])


    def test_download_existing_export_returns_file(self):
        job = self._create_job()
        export_zip = self.test_artifacts_dir / f'job_{job.id}_export.zip'
        export_zip.write_bytes(b'fake zip')
        with patch('app.api.jobs.clips.list_job_export_bundles', return_value=[{'name': export_zip.name, 'path': str(export_zip), 'size_bytes': export_zip.stat().st_size, 'modified_at': datetime.now(UTC)}]):
            response = self.client.get(f'/jobs/{job.id}/export/files/{export_zip.name}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['content-type'], 'application/zip')

