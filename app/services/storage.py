from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from app.core.config import settings


PRIVATE_STORAGE_BACKENDS = {"s3", "r2"}


def normalize_storage_key(*parts: str | int) -> str:
    raw = "/".join(str(part).strip().replace("\\", "/").strip("/") for part in parts)
    return "/".join(piece for piece in raw.split("/") if piece and piece != ".")


@dataclass(frozen=True)
class StorageObject:
    key: str
    path: str | None
    url: str | None = None
    size_bytes: int | None = None


class LocalStorage:
    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)

    def path_for(self, key: str) -> Path:
        normalized = normalize_storage_key(key)
        path = self.base_dir / normalized
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def ensure_prefix(self, key: str) -> Path:
        path = self.base_dir / normalize_storage_key(key)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def exists(self, key_or_path: str | None) -> bool:
        path = self.resolve_path(key_or_path)
        return bool(path and path.exists())

    def resolve_path(self, key_or_path: str | None) -> Path | None:
        if not key_or_path:
            return None
        candidate = Path(key_or_path)
        if candidate.is_absolute() or candidate.exists():
            return candidate
        return self.base_dir / normalize_storage_key(key_or_path)

    def key_for_path(self, file_path: str | Path) -> str | None:
        target = Path(file_path).resolve()
        try:
            return target.relative_to(self.base_dir.resolve()).as_posix()
        except ValueError:
            return None

    def public_url_for_path(self, file_path: str | Path) -> str | None:
        key = self.key_for_path(file_path)
        if not key:
            return None
        if settings.storage_public_base_url:
            return f"{settings.storage_public_base_url.rstrip('/')}/{key}"
        return f"/static/{key}"

    def list(self, prefix: str, pattern: str = "*") -> list[StorageObject]:
        directory = self.base_dir / normalize_storage_key(prefix)
        if not directory.exists():
            return []
        rows = []
        for path in directory.glob(pattern):
            stat = path.stat()
            rows.append(
                StorageObject(
                    key=self.key_for_path(path) or path.name,
                    path=str(path),
                    url=self.public_url_for_path(path),
                    size_bytes=stat.st_size,
                )
            )
        return rows

    def delete(self, key_or_path: str | None) -> bool:
        path = self.resolve_path(key_or_path)
        if not path or not path.exists() or not path.is_file():
            return False
        path.unlink()
        return True

    def ensure_default_prefixes(self, prefixes: Iterable[str]) -> None:
        for prefix in prefixes:
            self.ensure_prefix(prefix)


def get_storage() -> LocalStorage:
    return LocalStorage(settings.base_data_dir)


def is_private_storage_enabled() -> bool:
    return settings.storage_backend in PRIVATE_STORAGE_BACKENDS
