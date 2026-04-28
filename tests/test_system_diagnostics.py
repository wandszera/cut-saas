import unittest
from unittest.mock import patch

from app.services.system_diagnostics import build_system_diagnostics


class SystemDiagnosticsTestCase(unittest.TestCase):
    def test_deployment_readiness_flags_local_defaults_as_pending(self):
        diagnostics = build_system_diagnostics()

        readiness = diagnostics["deployment_readiness"]
        self.assertFalse(readiness["ready"])
        self.assertGreater(readiness["checks_total"], 0)
        self.assertTrue(any(item["status"] == "pendente" for item in readiness["checks"]))

    def test_deployment_readiness_marks_staging_stack_as_ready(self):
        with patch("app.services.system_diagnostics.settings") as mocked_settings:
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


if __name__ == "__main__":
    unittest.main()
