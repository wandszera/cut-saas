import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.models.candidate import Candidate
from app.models.clip import Clip
from app.models.job import Job
from app.models.job_step import JobStep
from app.models.niche_definition import NicheDefinition
from app.models.niche_keyword import NicheKeyword
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.worker import get_next_pending_job_id, run_worker, run_worker_once


class WorkerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_artifacts_dir = Path("test_databases")
        cls.test_artifacts_dir.mkdir(parents=True, exist_ok=True)
        cls.db_path = cls.test_artifacts_dir / f"worker_{uuid4().hex}.db"
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

    def _create_job(self, *, status: str = "pending", title: str = "Job") -> Job:
        db = self.TestingSessionLocal()
        try:
            job = Job(
                source_type="youtube",
                source_value="https://www.youtube.com/watch?v=abc123def45",
                status=status,
                title=title,
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            job_id = job.id
        finally:
            db.close()

        return Job(id=job_id, source_type="youtube", source_value="", status=status, title=title)

    def test_get_next_pending_job_id_returns_oldest_pending_job(self):
        failed_job = self._create_job(status="failed", title="Failed")
        first_pending = self._create_job(status="pending", title="First")
        second_pending = self._create_job(status="pending", title="Second")

        db = self.TestingSessionLocal()
        try:
            self.assertEqual(get_next_pending_job_id(db), first_pending.id)
        finally:
            db.close()

        db = self.TestingSessionLocal()
        try:
            self.assertNotEqual(get_next_pending_job_id(db), failed_job.id)
        finally:
            db.close()
        self.assertLess(first_pending.id, second_pending.id)

    def test_run_worker_once_processes_pending_job_outside_web_process(self):
        job = self._create_job(status="pending")

        with (
            patch("app.worker.SessionLocal", self.TestingSessionLocal),
            patch("app.worker.process_job_pipeline") as mocked_pipeline,
        ):
            did_work = run_worker_once()

        self.assertTrue(did_work)
        mocked_pipeline.assert_called_once()
        self.assertEqual(mocked_pipeline.call_args.args[0], job.id)
        self.assertIn("worker_id", mocked_pipeline.call_args.kwargs)

    def test_run_worker_once_returns_false_when_queue_is_empty(self):
        self._create_job(status="done")

        with (
            patch("app.worker.SessionLocal", self.TestingSessionLocal),
            patch("app.worker.process_job_pipeline") as mocked_pipeline,
        ):
            did_work = run_worker_once()

        self.assertFalse(did_work)
        mocked_pipeline.assert_not_called()

    def test_run_worker_once_logs_idle_when_queue_is_empty(self):
        self._create_job(status="done")

        with (
            patch("app.worker.SessionLocal", self.TestingSessionLocal),
            patch("app.worker.process_job_pipeline") as mocked_pipeline,
            patch("app.worker._log_worker_event") as mocked_log,
        ):
            did_work = run_worker_once()

        self.assertFalse(did_work)
        mocked_pipeline.assert_not_called()
        mocked_log.assert_any_call("worker_idle")

    def test_run_worker_logs_loop_lifecycle(self):
        with (
            patch("app.worker.run_worker_once", side_effect=[False]),
            patch("app.worker._log_worker_event") as mocked_log,
        ):
            processed = run_worker(poll_interval_seconds=0.1, max_jobs=1)

        self.assertEqual(processed, 0)
        mocked_log.assert_any_call("worker_loop_started", poll_interval_seconds=0.1, max_jobs=1)
        mocked_log.assert_any_call("worker_loop_finished", processed_jobs=0)


if __name__ == "__main__":
    unittest.main()
