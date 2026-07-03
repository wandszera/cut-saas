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
class RoutesTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_artifacts_dir = Path('test_databases')
        cls.test_artifacts_dir.mkdir(parents=True, exist_ok=True)
        cls.db_path = cls.test_artifacts_dir / f'test_{uuid4().hex}.db'
        cls.engine = create_engine(f'sqlite:///{cls.db_path}', connect_args={'check_same_thread': False})
        cls.TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)
        import app.services.pipeline as pipeline_module
        cls._original_session_local = pipeline_module.SessionLocal
        pipeline_module.SessionLocal = cls.TestingSessionLocal
    
        def override_get_db():
            db = cls.TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()
        app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(app)


    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()
        import app.services.pipeline as pipeline_module
        pipeline_module.SessionLocal = cls._original_session_local
        cls.engine.dispose()
        if cls.db_path.exists():
            cls.db_path.unlink()


    def setUp(self):
        self.client.cookies.clear()
        Base.metadata.drop_all(bind=self.engine)
        Base.metadata.create_all(bind=self.engine)
        db = self._session()
        try:
            user, workspace, _membership = create_user_with_workspace(db, email=f'routes-{uuid4().hex}@example.com', password_hash='hashed-password', workspace_name='Routes Workspace')
            db.add(Subscription(workspace_id=workspace.id, provider='mock', provider_checkout_id=f'cs_routes_free_{uuid4().hex}', provider_customer_id=f'cus_routes_free_{uuid4().hex}', plan_slug='free', status='active'))
            db.commit()
            db.refresh(user)
            db.refresh(workspace)
            self.user_id = user.id
            self.workspace_id = workspace.id
            self.client.cookies.set('cut_saas_session', create_session_token(user.id))
            self.client.cookies.set('cut_saas_csrf', 'test-csrf-token')
            self.client.headers.update({'X-CSRF-Token': 'test-csrf-token'})
        finally:
            db.close()


    def _session(self):
        return self.TestingSessionLocal()


    def _create_job(self, **overrides) -> Job:
        payload = {'workspace_id': self.workspace_id, 'source_type': 'youtube', 'source_value': 'https://www.youtube.com/watch?v=abc123def45', 'status': 'done', 'title': 'Video de teste', 'video_path': 'C:/tmp/video.mp4', 'audio_path': 'C:/tmp/audio.mp3', 'transcript_path': 'C:/tmp/transcript.json', 'detected_niche': 'podcast'}
        payload.update(overrides)
        db = self._session()
        try:
            job = Job(**payload)
            db.add(job)
            db.commit()
            db.refresh(job)
            db.expunge(job)
            return job
        finally:
            db.close()


    def _create_candidate(self, job_id: int, **overrides) -> Candidate:
        payload = {'job_id': job_id, 'mode': 'short', 'start_time': 10.0, 'end_time': 70.0, 'duration': 60.0, 'score': 9.2, 'reason': 'gancho forte', 'opening_text': 'abertura', 'closing_text': 'fechamento', 'full_text': 'texto completo', 'hook_score': 2.0, 'clarity_score': 1.5, 'closure_score': 1.0, 'emotion_score': 0.5, 'duration_fit_score': 3.0, 'transcript_context_score': 0.0, 'llm_score': None, 'llm_why': None, 'llm_title': None, 'llm_hook': None, 'status': 'pending'}
        payload.update(overrides)
        db = self._session()
        try:
            candidate = Candidate(**payload)
            db.add(candidate)
            db.commit()
            db.refresh(candidate)
            db.expunge(candidate)
            return candidate
        finally:
            db.close()


    def _create_clip(self, job_id: int, **overrides) -> Clip:
        payload = {'job_id': job_id, 'source': 'candidate', 'mode': 'short', 'start_time': 10.0, 'end_time': 70.0, 'duration': 60.0, 'score': 9.2, 'reason': 'gancho forte', 'text': 'texto do clip', 'headline': 'Titulo sugerido', 'description': 'Descricao curta', 'hashtags': '#cortes #shorts', 'suggested_filename': 'clip-sugerido.mp4', 'render_preset': 'clean', 'publication_status': 'draft', 'subtitles_burned': False, 'output_path': 'C:/tmp/clip.mp4'}
        payload.update(overrides)
        db = self._session()
        try:
            clip = Clip(**payload)
            db.add(clip)
            db.commit()
            db.refresh(clip)
            db.expunge(clip)
            return clip
        finally:
            db.close()


    def _create_niche_definition(self, **overrides) -> NicheDefinition:
        payload = {'workspace_id': self.workspace_id, 'name': 'Nicho Custom', 'slug': 'nicho-custom', 'description': 'Descricao de teste', 'keywords_json': '["keyword1","keyword2","keyword3"]', 'weights_json': '{"hook": 1.1, "clarity": 1.1, "niche_bonus": 1.2}', 'source': 'custom', 'status': 'pending', 'llm_notes': 'Sugestao de teste'}
        payload.update(overrides)
        db = self._session()
        try:
            niche = NicheDefinition(**payload)
            db.add(niche)
            db.commit()
            db.refresh(niche)
            db.expunge(niche)
            return niche
        finally:
            db.close()

