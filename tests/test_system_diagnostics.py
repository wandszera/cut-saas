import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base
from app.models import candidate as _candidate_model  # noqa: F401
from app.models.job import Job
from app.models.job_step import JobStep
from app.models import subscription as _subscription_model  # noqa: F401
from app.models import usage_event as _usage_event_model  # noqa: F401
from app.models.user import User  # noqa: F401
from app.models.workspace import Workspace
from app.models import workspace_member as _workspace_member_model  # noqa: F401
from app.services.system_diagnostics import build_runtime_readiness, build_system_diagnostics


class FakeVersionInfo:
    major = 3
    minor = 13

    def __lt__(self, other):
        return (self.major, self.minor) < other


class SystemDiagnosticsTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        cls.TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        Base.metadata.drop_all(bind=self.engine)
        Base.metadata.create_all(bind=self.engine)

    def test_deployment_readiness_flags_local_defaults_as_pending(self):
        diagnostics = build_system_diagnostics()

        readiness = diagnostics["deployment_readiness"]
        self.assertFalse(readiness["ready"])
        self.assertGreater(readiness["checks_total"], 0)
        self.assertTrue(any(item["status"] == "pendente" for item in readiness["checks"]))

    def test_deployment_readiness_marks_staging_stack_as_ready(self):
        with (
            patch("app.services.system_diagnostics.settings") as mocked_settings,
            patch("app.services.system_diagnostics._transcription_runtime_compatibility_item", return_value={"name": "Transcription runtime", "ok": True, "status": "ok", "detail": "ok"}),
        ):
            mocked_settings.environment = "staging"
            mocked_settings.is_deployed_environment = True
            mocked_settings.database_url_for_engine = "postgresql+psycopg://cut:secret@db:5432/cut_saas"
            mocked_settings.secret_key = "a-very-strong-staging-secret-key-123"
            mocked_settings.session_cookie_secure = False
            mocked_settings.pipeline_queue_backend = "worker"
            mocked_settings.storage_backend = "s3"
            mocked_settings.billing_provider = "stripe"
            mocked_settings.database_url = "postgresql://cut:secret@db:5432/cut_saas"
            mocked_settings.base_data_dir = "./data"
            mocked_settings.whisper_model = "base"
            mocked_settings.llm_provider = "ollama"
            mocked_settings.llm_model = "qwen2.5:7b"
            mocked_settings.llm_rerank_enabled = False
            mocked_settings.node_bin = "node"
            mocked_settings.ollama_base_url = "http://127.0.0.1:11434"
            mocked_settings.openai_base_url = "https://api.openai.com/v1"

            diagnostics = build_system_diagnostics()

        readiness = diagnostics["deployment_readiness"]
        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["checks_ok"], readiness["checks_total"])

    def test_deployment_readiness_flags_incompatible_runtime_for_faster_whisper(self):
        with (
            patch("app.services.system_diagnostics.importlib.util.find_spec", return_value=None),
            patch("app.services.system_diagnostics.settings") as mocked_settings,
            patch("app.services.system_diagnostics.sys.version_info", FakeVersionInfo()),
        ):
            mocked_settings.environment = "staging"
            mocked_settings.is_deployed_environment = True
            mocked_settings.database_url_for_engine = "postgresql+psycopg://cut:secret@db:5432/cut_saas"
            mocked_settings.secret_key = "a-very-strong-staging-secret-key-123"
            mocked_settings.session_cookie_secure = False
            mocked_settings.pipeline_queue_backend = "worker"
            mocked_settings.storage_backend = "s3"
            mocked_settings.billing_provider = "stripe"
            mocked_settings.transcription_provider = "auto"

            readiness = build_system_diagnostics()["deployment_readiness"]

        runtime_item = next(item for item in readiness["checks"] if item["name"] == "Transcription runtime")
        self.assertFalse(runtime_item["ok"])
        self.assertIn("requer Python 3.11 ou 3.12 para faster_whisper", runtime_item["detail"])

    def test_runtime_readiness_includes_ffprobe_and_queue_mode(self):
        with (
            patch("app.services.system_diagnostics._detect_database", return_value={"name": "Banco", "ok": True, "status": "ok", "detail": "db"}),
            patch("app.services.system_diagnostics._detect_node", return_value={"name": "Node.js", "ok": True, "status": "ok", "detail": "node"}),
            patch("app.services.system_diagnostics._detect_ffmpeg", return_value={"name": "FFmpeg", "ok": True, "status": "ok", "detail": "ffmpeg"}),
            patch("app.services.system_diagnostics._detect_ffprobe", return_value={"name": "FFprobe", "ok": True, "status": "ok", "detail": "ffprobe"}),
            patch("app.services.system_diagnostics._detect_whisper", return_value={"name": "Transcricao", "ok": True, "status": "ok", "detail": "provider=auto"}),
            patch("app.services.system_diagnostics.settings") as mocked_settings,
        ):
            mocked_settings.pipeline_queue_backend = "worker"
            readiness = build_runtime_readiness()

        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["checks_ok"], readiness["checks_total"])
        self.assertTrue(any(item["name"] == "FFprobe" for item in readiness["checks"]))
        self.assertTrue(any(item["name"] == "Worker queue mode" for item in readiness["checks"]))

    def test_runtime_readiness_reports_stale_worker_steps(self):
        db = self.TestingSessionLocal()
        try:
            job = Job(source_type="youtube", source_value="https://example.com/video", status="transcribing")
            db.add(job)
            db.commit()
            db.refresh(job)
            stale_heartbeat = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
            db.add(
                JobStep(
                    job_id=job.id,
                    step_name="transcribing",
                    status="running",
                    attempts=1,
                    details=f'{{"heartbeat_at": "{stale_heartbeat}"}}',
                )
            )
            db.commit()
        finally:
            db.close()

        with (
            patch("app.services.system_diagnostics.SessionLocal", self.TestingSessionLocal),
            patch("app.services.system_diagnostics._detect_database", return_value={"name": "Banco", "ok": True, "status": "ok", "detail": "db"}),
            patch("app.services.system_diagnostics._detect_node", return_value={"name": "Node.js", "ok": True, "status": "ok", "detail": "node"}),
            patch("app.services.system_diagnostics._detect_ffmpeg", return_value={"name": "FFmpeg", "ok": True, "status": "ok", "detail": "ffmpeg"}),
            patch("app.services.system_diagnostics._detect_ffprobe", return_value={"name": "FFprobe", "ok": True, "status": "ok", "detail": "ffprobe"}),
            patch("app.services.system_diagnostics._detect_whisper", return_value={"name": "Transcricao", "ok": True, "status": "ok", "detail": "provider=auto"}),
            patch("app.services.system_diagnostics._storage_readiness_item", return_value={"name": "Storage access", "ok": True, "status": "ok", "detail": "local"}),
            patch("app.services.system_diagnostics.settings") as mocked_settings,
        ):
            mocked_settings.pipeline_queue_backend = "worker"
            readiness = build_runtime_readiness()

        worker_item = next(item for item in readiness["checks"] if item["name"] == "Worker backlog")
        self.assertFalse(worker_item["ok"])
        self.assertIn("stale_running_steps=1", worker_item["detail"])

    def test_transcription_diagnostics_reports_fallback_when_faster_whisper_is_unavailable(self):
        with (
            patch("app.services.system_diagnostics.importlib.util.find_spec") as mocked_find_spec,
            patch("app.services.system_diagnostics._resolve_transcription_provider", return_value="openai_whisper"),
            patch("app.services.system_diagnostics.settings") as mocked_settings,
            patch("app.services.system_diagnostics.sys.version_info", FakeVersionInfo()),
        ):
            mocked_settings.transcription_provider = "auto"
            mocked_settings.whisper_model = "base"
            mocked_settings.whisper_precision = "auto"

            def _fake_find_spec(name: str):
                if name == "whisper":
                    return object()
                if name == "faster_whisper":
                    return None
                return None

            mocked_find_spec.side_effect = _fake_find_spec
            item = build_system_diagnostics()["checks"][4]

        self.assertEqual(item["name"], "Transcricao")
        self.assertTrue(item["ok"])
        self.assertIn("provider=auto", item["detail"])
        self.assertIn("resolved_provider=openai_whisper", item["detail"])
        self.assertIn("python=3.13", item["detail"])
        self.assertIn("python_incompativel_para_faster_whisper", item["detail"])
        self.assertIn("faster_whisper indisponivel", item["detail"])


if __name__ == "__main__":
    unittest.main()
