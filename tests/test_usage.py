import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.models.candidate import Candidate
from app.models.job import Job
from app.models.usage_event import UsageEvent
from app.services.accounts import create_user_with_workspace
from app.services import storage
from app.services.render_workflow import render_candidate_clip
from app.services.storage import LocalStorage
from app.services.usage import (
    calculate_workspace_storage_usage,
    record_storage_snapshot_usage,
    record_video_processed_usage,
)


class UsageTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_artifacts_dir = Path("test_databases")
        cls.test_artifacts_dir.mkdir(parents=True, exist_ok=True)
        cls.db_path = cls.test_artifacts_dir / f"usage_{uuid4().hex}.db"
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

    def _create_workspace_job(self, db):
        _user, workspace, _membership = create_user_with_workspace(
            db,
            email=f"usage-{uuid4().hex}@example.com",
            password_hash="hashed-password",
            workspace_name="Usage",
        )
        job = Job(
            workspace_id=workspace.id,
            source_type="youtube",
            source_value="https://www.youtube.com/watch?v=abc123def45",
            status="done",
        )
        db.add(job)
        db.commit()
        db.refresh(workspace)
        db.refresh(job)
        return workspace, job

    def test_record_video_processed_usage_is_idempotent_by_job(self):
        db = self.TestingSessionLocal()
        try:
            workspace, job = self._create_workspace_job(db)

            record_video_processed_usage(db, job, duration_seconds=125)
            record_video_processed_usage(db, job, duration_seconds=125)

            events = db.query(UsageEvent).filter(UsageEvent.workspace_id == workspace.id).all()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].event_type, "video_processed")
            self.assertEqual(events[0].unit, "minute")
            self.assertAlmostEqual(events[0].quantity, 2.0833)
        finally:
            db.close()

    def test_render_candidate_clip_records_render_usage_event(self):
        db = self.TestingSessionLocal()
        try:
            _workspace, job = self._create_workspace_job(db)
            candidate = Candidate(
                job_id=job.id,
                mode="short",
                start_time=0,
                end_time=30,
                duration=30,
                score=9,
                status="pending",
            )
            db.add(candidate)
            db.commit()
            db.refresh(candidate)
            output_path = LocalStorage(self.base_dir).path_for("clips/job_1/clip.mp4")
            output_path.write_bytes(b"clip")

            with patch("app.services.render_workflow.render_clip", return_value=str(output_path)):
                clip, _subtitles_path, _output_path = render_candidate_clip(
                    db=db,
                    job=job,
                    candidate=candidate,
                    burn_subtitles=False,
                    render_preset="clean",
                )

            event = db.query(UsageEvent).filter(UsageEvent.event_type == "render").one()
            self.assertEqual(event.job_id, job.id)
            self.assertEqual(event.quantity, 1)
            self.assertIn(f"clip:{clip.id}:render", event.idempotency_key)
        finally:
            db.close()

    def test_storage_snapshot_usage_records_current_workspace_bytes(self):
        db = self.TestingSessionLocal()
        try:
            workspace, job = self._create_workspace_job(db)
            video_path = LocalStorage(self.base_dir).path_for("downloads/job_1.mp4")
            video_path.write_bytes(b"12345")
            job.video_path = str(video_path)
            db.commit()

            usage = calculate_workspace_storage_usage(db, workspace.id)
            event = record_storage_snapshot_usage(db, workspace.id)

            self.assertEqual(usage.total_bytes, 5)
            self.assertEqual(event.event_type, "storage_snapshot")
            self.assertEqual(event.quantity, 5)
            self.assertEqual(event.unit, "byte")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
