from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Video Cuts Backend"
    debug: bool = True
    database_url: str = "sqlite:///./video_cuts.db"
    base_data_dir: str = "./data"

    ytdlp_cookies_file: str | None = None
    ytdlp_cookies_browser: str | None = None
    ytdlp_cookies_browser_profile: str | None = None
    ytdlp_verbose: bool = True
    whisper_model: str = "base"
    node_bin: str = "node"
    node_extra_path: str | None = None
    llm_rerank_enabled: bool = False
    llm_provider: str = "ollama"
    llm_model: str = "qwen2.5:7b"
    llm_top_n: int = 12
    ollama_base_url: str = "http://127.0.0.1:11434"
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
