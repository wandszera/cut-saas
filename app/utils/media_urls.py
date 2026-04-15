from pathlib import Path

from app.core.config import settings


def build_static_url(file_path: str | None) -> str | None:
    if not file_path:
        return None

    base_dir = Path(settings.base_data_dir).resolve()
    target = Path(file_path).resolve()

    try:
        relative = target.relative_to(base_dir)
    except ValueError:
        return None

    return f"/static/{relative.as_posix()}"