import base64
import hashlib
import hmac
import time

from app.core.config import settings
from app.services.storage import get_storage


def _media_signature(payload: str) -> str:
    digest = hmac.new(
        settings.secret_key.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def create_media_access_token(file_path: str, *, max_age_seconds: int = 900) -> str | None:
    key = get_storage().key_for_path(file_path)
    if not key:
        return None
    expires_at = int(time.time()) + int(max_age_seconds)
    payload = f"{expires_at}:{key}"
    signature = _media_signature(payload)
    raw_token = f"{payload}:{signature}"
    return base64.urlsafe_b64encode(raw_token.encode("utf-8")).decode("ascii").rstrip("=")


def parse_media_access_token(token: str | None) -> str | None:
    if not token:
        return None
    try:
        padded = token + "=" * (-len(token) % 4)
        raw_token = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        expires_at_raw, key, signature = raw_token.split(":", 2)
        expires_at = int(expires_at_raw)
    except (ValueError, UnicodeDecodeError):
        return None
    payload = f"{expires_at}:{key}"
    expected_signature = _media_signature(payload)
    if not hmac.compare_digest(signature, expected_signature):
        return None
    if expires_at < int(time.time()):
        return None
    return key


def build_static_url(file_path: str | None) -> str | None:
    if not file_path:
        return None

    token = create_media_access_token(file_path)
    if not token:
        return None
    return f"/files/download/{token}"
