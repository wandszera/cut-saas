from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable, Protocol

import boto3
from botocore.exceptions import BotoCoreError, ClientError

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


class StorageBackend(Protocol):
    def path_for(self, key: str) -> Path: ...
    def ensure_prefix(self, key: str) -> Path: ...
    def exists(self, key_or_path: str | None) -> bool: ...
    def resolve_path(self, key_or_path: str | None) -> Path | None: ...
    def key_for_path(self, file_path: str | Path) -> str | None: ...
    def public_url_for_path(self, file_path: str | Path) -> str | None: ...
    def list(self, prefix: str, pattern: str = "*") -> list[StorageObject]: ...
    def delete(self, key_or_path: str | None) -> bool: ...
    def ensure_default_prefixes(self, prefixes: Iterable[str]) -> None: ...
    def sync_path(self, key_or_path: str | Path) -> str: ...


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

    def sync_path(self, key_or_path: str | Path) -> str:
        path = self.resolve_path(str(key_or_path))
        if not path:
            raise FileNotFoundError(f"Arquivo nao encontrado para sync: {key_or_path}")
        return str(path)


class S3Storage(LocalStorage):
    def __init__(
        self,
        *,
        base_dir: str | Path,
        bucket: str,
        endpoint_url: str | None = None,
        region_name: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
    ):
        super().__init__(base_dir)
        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            region_name=region_name or None,
            aws_access_key_id=access_key_id or None,
            aws_secret_access_key=secret_access_key or None,
        )

    def _key_from_path_or_key(self, key_or_path: str | Path | None) -> str | None:
        if not key_or_path:
            return None
        path = super().resolve_path(str(key_or_path))
        if path and path.exists():
            key = super().key_for_path(path)
            if key:
                return key
        raw = str(key_or_path).replace("\\", "/")
        if "://" in raw:
            return None
        return normalize_storage_key(raw)

    def _download_to_cache(self, key: str) -> Path | None:
        local_path = super().path_for(key)
        if local_path.exists():
            return local_path
        try:
            self.client.download_file(self.bucket, key, str(local_path))
            return local_path
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise

    def sync_path(self, key_or_path: str | Path) -> str:
        key = self._key_from_path_or_key(key_or_path)
        path = super().resolve_path(str(key_or_path))
        if not key or not path or not path.exists():
            raise FileNotFoundError(f"Arquivo nao encontrado para sync: {key_or_path}")
        self.client.upload_file(str(path), self.bucket, key)
        return key

    def exists(self, key_or_path: str | None) -> bool:
        key = self._key_from_path_or_key(key_or_path)
        if not key:
            return False
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def resolve_path(self, key_or_path: str | None) -> Path | None:
        key = self._key_from_path_or_key(key_or_path)
        if not key:
            return None
        return self._download_to_cache(key)

    def public_url_for_path(self, file_path: str | Path) -> str | None:
        key = self.key_for_path(file_path)
        if not key:
            return None
        if settings.storage_public_base_url:
            return f"{settings.storage_public_base_url.rstrip('/')}/{key}"
        return None

    def list(self, prefix: str, pattern: str = "*") -> list[StorageObject]:
        normalized_prefix = normalize_storage_key(prefix)
        rows: list[StorageObject] = []
        continuation_token: str | None = None

        while True:
            kwargs = {
                "Bucket": self.bucket,
                "Prefix": f"{normalized_prefix}/" if normalized_prefix else "",
                "MaxKeys": 1000,
            }
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            response = self.client.list_objects_v2(**kwargs)
            for item in response.get("Contents", []):
                key = item["Key"]
                filename = key.split("/")[-1]
                if not fnmatch(filename, pattern):
                    continue
                cached_path = self.resolve_path(key)
                rows.append(
                    StorageObject(
                        key=key,
                        path=str(cached_path) if cached_path else None,
                        url=self.public_url_for_path(cached_path or self.path_for(key)),
                        size_bytes=int(item.get("Size") or 0),
                    )
                )
            if not response.get("IsTruncated"):
                break
            continuation_token = response.get("NextContinuationToken")
        return rows

    def delete(self, key_or_path: str | None) -> bool:
        key = self._key_from_path_or_key(key_or_path)
        if not key:
            return False
        local_path = super().resolve_path(key)
        if local_path and local_path.exists() and local_path.is_file():
            local_path.unlink()
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
            return True
        except (ClientError, BotoCoreError):
            return False


def get_storage() -> StorageBackend:
    if settings.storage_backend in {"s3", "r2"}:
        return S3Storage(
            base_dir=settings.base_data_dir,
            bucket=settings.storage_bucket or "",
            endpoint_url=settings.storage_endpoint_url,
            region_name=settings.storage_region,
            access_key_id=settings.storage_access_key_id,
            secret_access_key=settings.storage_secret_access_key,
        )
    return LocalStorage(settings.base_data_dir)


def is_private_storage_enabled() -> bool:
    return settings.storage_backend in PRIVATE_STORAGE_BACKENDS
