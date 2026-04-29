from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ProductionEnvironment = Literal["local", "test", "staging", "production"]
PipelineQueueBackend = Literal["local", "worker"]
StorageBackend = Literal["local", "s3", "r2"]
BillingProvider = Literal["mock", "stripe", "mercado_pago"]
WhisperPrecisionMode = Literal["auto", "fp32", "fp16"]
TranscriptionProvider = Literal["auto", "openai_whisper", "faster_whisper"]


def normalize_database_url(database_url: str) -> str:
    if database_url.startswith("postgres://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgres://")
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
    return database_url


def is_postgres_url(database_url: str) -> bool:
    return database_url.startswith(("postgres://", "postgresql://", "postgresql+"))


class Settings(BaseSettings):
    app_name: str = "Video Cuts Backend"
    environment: ProductionEnvironment = "local"
    debug: bool = False
    database_url: str = "sqlite:///./video_cuts.db"
    base_data_dir: str = "./data"
    storage_backend: StorageBackend = "local"
    storage_bucket: str | None = None
    storage_public_base_url: str | None = None
    storage_endpoint_url: str | None = None
    storage_region: str | None = None
    storage_access_key_id: str | None = None
    storage_secret_access_key: str | None = None
    billing_provider: BillingProvider = "mock"
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_price_starter: str | None = None
    mercado_pago_access_token: str | None = None
    mercado_pago_webhook_secret: str | None = None
    artifact_retention_days: int = 30
    preserve_approved_artifacts: bool = True
    secret_key: str = "dev-secret-change-me"
    session_cookie_name: str = "cut_saas_session"
    session_max_age_seconds: int = 60 * 60 * 24 * 14
    session_cookie_secure: bool = False
    allowed_hosts: str = "localhost,127.0.0.1,testserver"
    proxy_trusted_hosts: str = "127.0.0.1"

    ytdlp_cookies_file: str | None = None
    ytdlp_cookies_browser: str | None = None
    ytdlp_cookies_browser_profile: str | None = None
    ytdlp_verbose: bool = True
    transcription_provider: TranscriptionProvider = "auto"
    whisper_model: str = "base"
    whisper_precision: WhisperPrecisionMode = "auto"
    node_bin: str = "node"
    node_extra_path: str | None = None
    llm_rerank_enabled: bool = False
    llm_provider: str = "ollama"
    llm_model: str = "qwen2.5:7b"
    llm_top_n: int = 12
    llm_timeout_seconds: float = 20.0
    max_concurrent_pipeline_jobs: int = 1
    pipeline_queue_backend: PipelineQueueBackend = "local"
    pipeline_lock_stale_seconds: int = 60 * 60
    short_min_duration_seconds: float = 20.0
    short_max_duration_seconds: float = 180.0
    long_min_duration_seconds: float = 300.0
    long_max_duration_seconds: float = 900.0
    short_min_candidates_per_job: int = 12
    short_max_candidates_per_job: int = 60
    long_min_candidates_per_job: int = 3
    long_max_candidates_per_job: int = 20
    candidate_duplicate_time_tolerance_seconds: float = 5.0
    candidate_relaxed_time_tolerance_seconds: float = 1.0
    candidate_duplicate_overlap_ratio: float = 0.9
    candidate_relaxed_overlap_ratio: float = 0.97
    ollama_base_url: str = "http://127.0.0.1:11434"
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def database_url_for_engine(self) -> str:
        return normalize_database_url(self.database_url)

    @property
    def is_deployed_environment(self) -> bool:
        return self.environment in {"staging", "production"}

    @property
    def allowed_hosts_list(self) -> list[str]:
        return [host.strip() for host in self.allowed_hosts.split(",") if host.strip()]

    @property
    def proxy_trusted_hosts_list(self) -> list[str]:
        return [host.strip() for host in self.proxy_trusted_hosts.split(",") if host.strip()]

    @model_validator(mode="after")
    def validate_deployed_environment(self):
        if self.is_deployed_environment:
            if self.debug:
                raise ValueError("DEBUG must be false in staging and production")
            if not is_postgres_url(self.database_url):
                raise ValueError("DATABASE_URL must use Postgres in staging and production")
            if self.secret_key == "dev-secret-change-me" or len(self.secret_key) < 32:
                raise ValueError("SECRET_KEY must be unique and at least 32 characters in staging and production")
            if not self.allowed_hosts_list:
                raise ValueError("ALLOWED_HOSTS must define at least one host in staging and production")
            if "*" in self.allowed_hosts_list:
                raise ValueError("ALLOWED_HOSTS cannot use wildcard in staging and production")
        if self.environment == "production" and not self.session_cookie_secure:
            raise ValueError("SESSION_COOKIE_SECURE must be true in production")
        if self.storage_backend in {"s3", "r2"} and not self.storage_bucket:
            raise ValueError("STORAGE_BUCKET is required for s3/r2 storage backends")
        if self.storage_backend in {"s3", "r2"} and not self.storage_access_key_id:
            raise ValueError("STORAGE_ACCESS_KEY_ID is required for s3/r2 storage backends")
        if self.storage_backend in {"s3", "r2"} and not self.storage_secret_access_key:
            raise ValueError("STORAGE_SECRET_ACCESS_KEY is required for s3/r2 storage backends")
        if self.storage_backend == "r2" and not self.storage_endpoint_url:
            raise ValueError("STORAGE_ENDPOINT_URL is required for r2 storage backend")
        if self.billing_provider == "stripe" and not self.stripe_secret_key:
            raise ValueError("STRIPE_SECRET_KEY is required when BILLING_PROVIDER=stripe")
        if self.billing_provider == "stripe" and not self.stripe_price_starter:
            raise ValueError("STRIPE_PRICE_STARTER is required when BILLING_PROVIDER=stripe")
        if self.billing_provider == "mercado_pago" and not self.mercado_pago_access_token:
            raise ValueError("MERCADO_PAGO_ACCESS_TOKEN is required when BILLING_PROVIDER=mercado_pago")
        return self


settings = Settings()
