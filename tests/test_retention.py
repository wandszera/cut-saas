import unittest
from datetime import datetime, timedelta, UTC
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.models.clip import Clip
from app.models.job import Job
from app.services import storage
from app.services.retention import RetentionPolicy, cleanup_expired_workspace_artifacts
from app.services.storage import LocalStorage
from app.services.usage import calculate_workspace_storage_usage


class RetentionTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_artifacts_dir = Path("test_databases")
        cls.test_artifacts_dir.mkdir(parents=True, exist_ok=True)
        cls.db_path = cls.test_artifacts_dir / f"retention_{uuid4().hex}.db"
        cls.engine = create_engine(
            f"sqlite:///{cls.db_path}",
            connect_args={"check_same_thread": False},
        )
        cls.TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        if cls.db_path.exists():
            cls.db_path.unlink()

    def setUp(self):
        Base.metadata.drop_all(bind=self.engine)
        Base.metadata.create_all(bind=self.engine)
        self.original_base_dir = storage.settings.base_data_dir
        self.base_dir = self.test_artifacts_dir / f"files_{uuid4().hex}"
        storage.settings.base_data_dir = str(self.base_dir)

    def tearDown(self):
        storage.settings.base_data_dir = self.original_base_dir

    def _write_file(self, key: str, payload: bytes) -> str:
        path = LocalStorage(self.base_dir).path_for(key)
        path.write_bytes(payload)
        return str(path)

    def _create_job(self, db, *, workspace_id: int, days_old: int = 45, **paths) -> Job:
        job = Job(
            workspace_id=workspace_id,
            source_type="youtube",
            source_value="https://www.youtube.com/watch?v=abc123def45",
            status="done",
            created_at=datetime.now(UTC) - timedelta(days=days_old),
            updated_at=datetime.now(UTC) - timedelta(days=days_old),
            **paths,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    def test_calculate_workspace_storage_usage_counts_owned_artifacts(self):
        db = self.TestingSessionLocal()
        try:
            owned_video = self._write_file("downloads/job_1.mp4", b"12345")
            owned_clip = self._write_file("clips/job_1/clip.mp4", b"123")
            other_video = self._write_file("downloads/job_2.mp4", b"999999")
            owned_job = self._create_job(db, workspace_id=1, video_path=owned_video)
            self._create_job(db, workspace_id=2, video_path=other_video)
            db.add(
                Clip(
                    job_id=owned_job.id,
                    source="candidate",
                    mode="short",
                    start_time=0,
                    end_time=10,
                    duration=10,
                    output_path=owned_clip,
                )
            )
            db.commit()

            usage = calculate_workspace_storage_usage(db, workspace_id=1)

            self.assertEqual(usage.files_count, 2)
            self.assertEqual(usage.total_bytes, 8)
        finally:
            db.close()

    def test_cleanup_expired_workspace_artifacts_deletes_old_unprotected_files(self):
        db = self.TestingSessionLocal()
        try:
            video_path = self._write_file("downloads/job_1.mp4", b"video")
            clip_path = self._write_file("clips/job_1/clip.mp4", b"clip")
            job = self._create_job(db, workspace_id=1, video_path=video_path)
            db.add(
                Clip(
                    job_id=job.id,
                    source="candidate",
                    mode="short",
                    start_time=0,
                    end_time=10,
                    duration=10,
                    output_path=clip_path,
                    publication_status="draft",
                )
            )
            db.commit()

            report = cleanup_expired_workspace_artifacts(
                db,
                workspace_id=1,
                policy=RetentionPolicy(retention_days=30),
            )

            self.assertEqual(report.deleted_count, 2)
            self.assertFalse(Path(video_path).exists())
            self.assertFalse(Path(clip_path).exists())
            self.assertEqual(report.deleted_bytes, 9)
        finally:
            db.close()

    def test_cleanup_preserves_ready_or_published_clips_when_policy_requires_it(self):
        db = self.TestingSessionLocal()
        try:
            clip_path = self._write_file("clips/job_1/published.mp4", b"clip")
            job = self._create_job(db, workspace_id=1)
            db.add(
                Clip(
                    job_id=job.id,
                    source="candidate",
                    mode="short",
                    start_time=0,
                    end_time=10,
                    duration=10,
                    output_path=clip_path,
                    publication_status="published",
                )
            )
            db.commit()

            report = cleanup_expired_workspace_artifacts(
                db,
                workspace_id=1,
                policy=RetentionPolicy(retention_days=30, preserve_approved_artifacts=True),
            )

            self.assertEqual(report.deleted_count, 0)
            self.assertEqual(len(report.preserved), 1)
            self.assertTrue(Path(clip_path).exists())
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
