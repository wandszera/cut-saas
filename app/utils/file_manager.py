from pathlib import Path
from app.core.config import settings


def ensure_directories():
    base = Path(settings.base_data_dir)
    for folder in ["downloads", "transcripts", "clips", "temp"]:
        (base / folder).mkdir(parents=True, exist_ok=True)