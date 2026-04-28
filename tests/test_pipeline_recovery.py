import unittest
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.database import Base
from app.models.job import Job
from app.models.job_step import JobStep
from app.services.pipeline import (
    _try_acquire_job_lock,
    _utcnow,
    recover_stale_pipeline_jobs,
)


class PipelineRecoveryTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_artifacts_dir = Path("test_databases")
        cls.test_artifacts_dir.mkdir(parents=True, exist_ok=True)
        cls.db_path = cls.test_artifacts_dir / f"pipeline_recovery_{uuid4().hex}.db"
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

    def _create_job(self, *, status: str = "pending") -> int:
        db = self.TestingSessionLocal()
        try:
            job = Job(
                source_type="youtube",
                source_value="https://www.youtube.com/watch?v=abc123def45",
                status=status,
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            return job.id
        finally:
            db.close()

    def test_job_lock_blocks_second_worker_until_released_or_stale(self):
        job_id = self._create_job()

        first_db = self.TestingSessionLocal()
        second_db = self.TestingSessionLocal()
        try:
            self.assertTrue(_try_acquire_job_lock(first_db, job_id, "worker-a"))
            self.assertFalse(_try_acquire_job_lock(second_db, job_id, "worker-b"))
        finally:
            first_db.close()
            second_db.close()

    def test_recover_stale_pipeline_jobs_requeues_interrupted_job(self):
        job_id = self._create_job(status="transcribing")
        stale_locked_at = _utcnow() - timedelta(hours=2)

        db = self.TestingSessionLocal()
        try:
            job = db.query(Job).filter(Job.id == job_id).one()
            job.locked_at = stale_locked_at
            job.locked_by = "dead-worker"
            db.add(
                JobStep(
                    job_id=job.id,
                    step_name="transcribing",
                    status="running",
                    attempts=1,
                    started_at=stale_locked_at,
                )
            )
            db.commit()

            recovered_count = recover_stale_pipeline_jobs(db)

            db.refresh(job)
            step = db.query(JobStep).filter(JobStep.job_id == job.id).one()
            self.assertEqual(recovered_count, 1)
            self.assertEqual(job.status, "pending")
            self.assertIsNone(job.locked_at)
            self.assertIsNone(job.locked_by)
            self.assertIn("recuperado", job.error_message)
            self.assertEqual(step.status, "failed")
            self.assertIn("interrompida", step.error_message)
            self.assertIn("recovered_from_stale_lock", step.details)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
