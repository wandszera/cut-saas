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
    

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()