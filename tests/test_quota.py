import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes_files import router as files_router
from app.api.routes_jobs import router as jobs_router
from app.db.database import Base, get_db
from app.models.clip import Clip
from app.models.job import Job
from app.models.subscription import Subscription
from app.models.usage_event import UsageEvent
from app.services import storage
from app.services.accounts import create_user_with_workspace
from app.services.auth import create_session_token
from app.services.quota import ensure_workspace_can_start_job, get_workspace_quota_status
from app.services.storage import LocalStorage
from app.utils.media_urls import build_static_url


class QuotaTestCase(unittest.TestCase):
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
        cls.app.include_router(jobs_router)
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
        self.base_dir = Path("test_databases") / f"quota_files_{uuid4().hex}"
        storage.settings.base_data_dir = str(self.base_dir)
        self.user_id, self.workspace_id = self._create_user_workspace()
        self.client.cookies.set("cut_saas_session", create_session_token(self.user_id))

    def tearDown(self):
        storage.settings.base_data_dir = self.original_base_dir

    def _create_user_workspace(self) -> tuple[int, int]:
        db = self.TestingSessionLocal()
        try:
            user, workspace, _membership = create_user_with_workspace(
                db,
                email=f"quota-{uuid4().hex}@example.com",
                password_hash="hashed-password",
                workspace_name="Quota Workspace",
            )
            db.add(
                Subscription(
                    workspace_id=workspace.id,
                    provider="mock",
                    provider_checkout_id=f"cs_quota_free_{uuid4().hex}",
                    provider_customer_id=f"cus_quota_free_{uuid4().hex}",
                    plan_slug="free",
                    status="active",
                )
            )
            db.commit()
            db.refresh(user)
            db.refresh(workspace)
            return user.id, workspace.id
        finally:
            db.close()

    def _record_video_minutes(self, minutes: float, *, created_at: datetime | None = None) -> None:
        db = self.TestingSessionLocal()
        try:
            db.add(
                UsageEvent(
                    workspace_id=self.workspace_id,
                    event_type="video_processed",
                    quantity=minutes,
                    unit="minute",
                    idempotency_key=f"quota:{uuid4().hex}",
                    created_at=created_at or datetime.now(UTC),
                )
            )
            db.commit()
        finally:
            db.close()

    def _create_signed_clip_url(self) -> str:
        local_storage = LocalStorage(self.base_dir)
        clip_path = local_storage.path_for("clips/job_1/clip.mp4")
        clip_path.write_bytes(b"clip-bytes")

        db = self.TestingSessionLocal()
        try:
            job = Job(
                workspace_id=self.workspace_id,
                source_type="local",
                source_value=str(clip_path),
                status="done",
                video_path=str(clip_path),
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            db.add(
                Clip(
                    job_id=job.id,
                    source="candidate",
                    mode="short",
                    start_time=0,
                    end_time=10,
                    duration=10,
                    output_path=str(clip_path),
                )
            )
            db.commit()
        finally:
            db.close()
        return build_static_url(str(clip_path))

    def test_quota_status_warns_near_monthly_limit(self):
        self._record_video_minutes(48)
        db = self.TestingSessionLocal()
        try:
            status = get_workspace_quota_status(db, self.workspace_id)
            self.assertTrue(status.is_near_limit)
            self.assertFalse(status.is_exceeded)
            self.assertEqual(status.remaining_video_minutes, 12)
        finally:
            db.close()

    def test_paid_quota_uses_active_subscription_cycle(self):
        period_end = datetime.now(UTC) + timedelta(days=7)
        period_start = period_end - timedelta(days=30)
        self._record_video_minutes(90, created_at=period_start - timedelta(days=1))
        self._record_video_minutes(120, created_at=period_start + timedelta(days=1))

        db = self.TestingSessionLocal()
        try:
            db.add(
                Subscription(
                    workspace_id=self.workspace_id,
                    provider="stripe",
                    provider_checkout_id="cs_quota_cycle",
                    provider_customer_id="cus_quota_cycle",
                    provider_subscription_id="sub_quota_cycle",
                    plan_slug="starter",
                    status="active",
                    current_period_end=period_end,
                )
            )
            db.commit()

            status = get_workspace_quota_status(db, self.workspace_id)

            self.assertEqual(status.plan.slug, "starter")
            self.assertEqual(status.used_video_minutes, 120)
            self.assertEqual(status.limit_video_minutes, 600)
            self.assertEqual(status.period_end.replace(tzinfo=UTC), period_end)
        finally:
            db.close()

    def test_workspace_over_limit_cannot_start_new_job(self):
        self._record_video_minutes(60)
        db = self.TestingSessionLocal()
        try:
            with self.assertRaises(Exception) as ctx:
                ensure_workspace_can_start_job(db, self.workspace_id)
            self.assertEqual(ctx.exception.status_code, 402)
        finally:
            db.close()

        response = self.client.post(
            "/jobs/local",
            json={"video_path": "C:/missing/video.mp4", "title": "Blocked"},
        )
        self.assertEqual(response.status_code, 402)
        self.assertIn("Limite mensal", response.json()["detail"])

    def test_workspace_over_limit_can_still_download_existing_files(self):
        signed_url = self._create_signed_clip_url()
        self._record_video_minutes(60)

        response = self.client.get(signed_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"clip-bytes")


if __name__ == "__main__":
    unittest.main()
