import unittest
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes_files import router as files_router
from app.db.database import Base, get_db
from app.models.clip import Clip
from app.models.job import Job
from app.services.accounts import create_user_with_workspace
from app.services.auth import create_session_token
from app.services import storage
from app.services.storage import LocalStorage
from app.utils.media_urls import build_static_url


class FileAccessTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        cls.TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)

        def override_get_db():
            db = cls.TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        cls.app = FastAPI()
        cls.app.include_router(files_router)
        cls.app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.app.dependency_overrides.clear()
        cls.engine.dispose()

    def setUp(self):
        Base.metadata.drop_all(bind=self.engine)
        Base.metadata.create_all(bind=self.engine)
        self.client.cookies.clear()
        self.original_base_dir = storage.settings.base_data_dir
        self.base_dir = Path("test_databases") / f"files_{uuid4().hex}"
        storage.settings.base_data_dir = str(self.base_dir)

    def tearDown(self):
        storage.settings.base_data_dir = self.original_base_dir

    def _create_user_workspace(self, email: str):
        db = self.TestingSessionLocal()
        try:
            user, workspace, _membership = create_user_with_workspace(
                db,
                email=email,
                password_hash="hashed-password",
                workspace_name=email,
            )
            db.commit()
            db.refresh(user)
            db.refresh(workspace)
            return user.id, workspace.id
        finally:
            db.close()

    def _create_clip_file(self, workspace_id: int) -> str:
        local_storage = LocalStorage(self.base_dir)
        clip_path = local_storage.path_for("clips/job_1/clip.mp4")
        clip_path.write_bytes(b"clip-bytes")

        db = self.TestingSessionLocal()
        try:
            job = Job(
                workspace_id=workspace_id,
                source_type="youtube",
                source_value="https://www.youtube.com/watch?v=abc123def45",
                status="done",
                video_path=str(clip_path),
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            clip = Clip(
                job_id=job.id,
                source="candidate",
                mode="short",
                start_time=0,
                end_time=10,
                duration=10,
                output_path=str(clip_path),
            )
            db.add(clip)
            db.commit()
        finally:
            db.close()
        return build_static_url(str(clip_path))

    def test_signed_file_requires_authentication(self):
        _user_id, workspace_id = self._create_user_workspace("owner@example.com")
        signed_url = self._create_clip_file(workspace_id)

        response = self.client.get(signed_url)

        self.assertEqual(response.status_code, 401)

    def test_signed_file_allows_owning_workspace(self):
        user_id, workspace_id = self._create_user_workspace("owner@example.com")
        signed_url = self._create_clip_file(workspace_id)
        self.client.cookies.set("cut_saas_session", create_session_token(user_id))

        response = self.client.get(signed_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"clip-bytes")

    def test_signed_file_blocks_other_workspace(self):
        _owner_user_id, owner_workspace_id = self._create_user_workspace("owner@example.com")
        other_user_id, _other_workspace_id = self._create_user_workspace("other@example.com")
        signed_url = self._create_clip_file(owner_workspace_id)
        self.client.cookies.set("cut_saas_session", create_session_token(other_user_id))

        response = self.client.get(signed_url)

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
