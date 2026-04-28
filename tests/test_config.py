import unittest

from pydantic import ValidationError

from app.core.config import Settings, normalize_database_url


class ConfigTestCase(unittest.TestCase):
    def test_local_environment_allows_sqlite(self):
        settings = Settings(_env_file=None, environment="local", database_url="sqlite:///./local.db")

        self.assertEqual(settings.environment, "local")
        self.assertEqual(settings.database_url_for_engine, "sqlite:///./local.db")

    def test_production_requires_postgres_debug_false_and_secure_cookie(self):
        with self.assertRaises(ValidationError):
            Settings(
                _env_file=None,
                environment="production",
                debug=True,
                database_url="sqlite:///./prod.db",
                secret_key="dev-secret-change-me",
                session_cookie_secure=False,
            )

    def test_staging_accepts_strong_secret_and_postgres(self):
        settings = Settings(
            _env_file=None,
            environment="staging",
            debug=False,
            database_url="postgresql://cut:secret@db:5432/cut_saas",
            secret_key="a-strong-staging-secret-key-32chars",
        )

        self.assertEqual(
            settings.database_url_for_engine,
            "postgresql+psycopg://cut:secret@db:5432/cut_saas",
        )

    def test_postgres_url_normalization_preserves_explicit_driver(self):
        self.assertEqual(
            normalize_database_url("postgresql+psycopg://cut:secret@db/cut_saas"),
            "postgresql+psycopg://cut:secret@db/cut_saas",
        )

    def test_stripe_provider_requires_secret_key(self):
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, billing_provider="stripe")

        with self.assertRaises(ValidationError):
            Settings(_env_file=None, billing_provider="stripe", stripe_secret_key="sk_test_123")

        settings = Settings(
            _env_file=None,
            billing_provider="stripe",
            stripe_secret_key="sk_test_123",
            stripe_price_starter="price_test_starter",
        )
        self.assertEqual(settings.billing_provider, "stripe")

    def test_mercado_pago_provider_requires_access_token(self):
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, billing_provider="mercado_pago")

        settings = Settings(
            _env_file=None,
            billing_provider="mercado_pago",
            mercado_pago_access_token="mp_test_123",
        )
        self.assertEqual(settings.billing_provider, "mercado_pago")


if __name__ == "__main__":
    unittest.main()
