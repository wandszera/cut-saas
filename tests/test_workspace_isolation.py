import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes_jobs import router as jobs_router
from app.db.database import Base, get_db
from app.models.job import Job
from app.models.candidate import Candidate
from app.models.clip import Clip
from app.services.accounts import create_user_with_workspace
from app.services.auth import create_session_token


class WorkspaceIsolationTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        cls.TestingSessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=cls.engine,
        )

        def override_get_db():
            db = cls.TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        cls.app = FastAPI()
        cls.app.include_router(jobs_router)
        cls.app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.app.dependency_overrides.clear()
        cls.engine.dispose()

    def setUp(self):
        Base.metadata.drop_all(bind=self.engine)
        Base.metadata.create_all(bind=self.engine)

    def _session(self):
        return self.TestingSessionLocal()

    def _seed_workspace_with_job(self, email: str, title: str):
        db = self._session()
        try:
            user, workspace, _membership = create_user_with_workspace(
                db,
                email=email,
                password_hash="hashed-password",
                workspace_name=title,
            )
            job = Job(
                workspace_id=workspace.id,
                source_type="youtube",
                source_value="https://www.youtube.com/watch?v=abc123def45",
                status="done",
                title=title,
            )
            db.add(job)
            db.commit()
            db.refresh(user)
            db.refresh(workspace)
            db.refresh(job)
            user_id = user.id
            workspace_id = workspace.id
            job_id = job.id
            db.expunge_all()
            return user_id, workspace_id, job_id
        finally:
            db.close()

    def _create_candidate(self, job_id: int) -> int:
        db = self._session()
        try:
            candidate = Candidate(
                job_id=job_id,
                mode="short",
                start_time=10.0,
                end_time=70.0,
                duration=60.0,
                score=8.8,
                status="pending",
            )
            db.add(candidate)
            db.commit()
            db.refresh(candidate)
            candidate_id = candidate.id
            db.expunge(candidate)
            return candidate_id
        finally:
            db.close()

    def _create_clip(self, job_id: int) -> int:
        db = self._session()
        try:
            clip = Clip(
                job_id=job_id,
                source="candidate",
                mode="short",
                start_time=10.0,
                end_time=70.0,
                duration=60.0,
                output_path="C:/tmp/clip.mp4",
                publication_status="draft",
            )
            db.add(clip)
            db.commit()
            db.refresh(clip)
            clip_id = clip.id
            db.expunge(clip)
            return clip_id
        finally:
            db.close()

    def test_get_job_is_scoped_to_authenticated_workspace(self):
        first_user_id, first_workspace_id, first_job_id = self._seed_workspace_with_job(
            "first@example.com",
            "Primeiro Workspace",
        )
        second_user_id, _second_workspace_id, second_job_id = self._seed_workspace_with_job(
            "second@example.com",
            "Segundo Workspace",
        )

        self.client.cookies.set("cut_saas_session", create_session_token(first_user_id))
        own_response = self.client.get(f"/jobs/{first_job_id}")
        foreign_response = self.client.get(f"/jobs/{second_job_id}")

        self.assertEqual(own_response.status_code, 200)
        self.assertEqual(own_response.json()["workspace_id"], first_workspace_id)
        self.assertEqual(foreign_response.status_code, 404)

        self.client.cookies.set("cut_saas_session", create_session_token(second_user_id))
        second_response = self.client.get(f"/jobs/{second_job_id}")
        self.assertEqual(second_response.status_code, 200)

    def test_get_job_requires_authenticated_workspace(self):
        _user_id, _workspace_id, job_id = self._seed_workspace_with_job(
            "anon@example.com",
            "Anon Workspace",
        )

        response = self.client.get(f"/jobs/{job_id}")

        self.assertEqual(response.status_code, 401)

    def test_candidate_mutation_is_scoped_to_workspace(self):
        first_user_id, _first_workspace_id, _first_job_id = self._seed_workspace_with_job(
            "first@example.com",
            "Primeiro Workspace",
        )
        _second_user_id, _second_workspace_id, second_job_id = self._seed_workspace_with_job(
            "second@example.com",
            "Segundo Workspace",
        )
        foreign_candidate_id = self._create_candidate(second_job_id)

        self.client.cookies.set("cut_saas_session", create_session_token(first_user_id))
        response = self.client.post(f"/jobs/candidates/{foreign_candidate_id}/approve")

        self.assertEqual(response.status_code, 404)

    def test_clip_publication_is_scoped_to_workspace(self):
        first_user_id, _first_workspace_id, _first_job_id = self._seed_workspace_with_job(
            "first@example.com",
            "Primeiro Workspace",
        )
        _second_user_id, _second_workspace_id, second_job_id = self._seed_workspace_with_job(
            "second@example.com",
            "Segundo Workspace",
        )
        foreign_clip_id = self._create_clip(second_job_id)

        self.client.cookies.set("cut_saas_session", create_session_token(first_user_id))
        response = self.client.post(
            f"/jobs/clips/{foreign_clip_id}/publication",
            params={"status": "ready"},
        )

        self.assertEqual(response.status_code, 404)
