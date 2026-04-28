import base64
import hashlib
import hmac
import os
import secrets
import time

from fastapi import Request, Response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.user import User
from app.services.accounts import create_user_with_workspace, normalize_email


PASSWORD_ITERATIONS = 260_000


def hash_password(password: str) -> str:
    if not password or len(password) < 8:
        raise ValueError("A senha deve ter pelo menos 8 caracteres")

    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return "pbkdf2_sha256${}${}${}".format(
        PASSWORD_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations_raw, salt_raw, expected_raw = password_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_raw)
        salt = base64.urlsafe_b64decode(salt_raw.encode("ascii"))
        expected = base64.urlsafe_b64decode(expected_raw.encode("ascii"))
    except (ValueError, TypeError):
        return False

    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual, expected)


def _session_signature(payload: str) -> str:
    digest = hmac.new(
        settings.secret_key.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def create_session_token(user_id: int, *, max_age_seconds: int | None = None) -> str:
    expires_at = int(time.time()) + int(max_age_seconds or settings.session_max_age_seconds)
    nonce = secrets.token_urlsafe(12)
    payload = f"{int(user_id)}:{expires_at}:{nonce}"
    signature = _session_signature(payload)
    return f"{payload}:{signature}"


def parse_session_token(token: str | None) -> int | None:
    if not token:
        return None
    parts = token.split(":")
    if len(parts) != 4:
        return None
    user_id_raw, expires_at_raw, nonce, signature = parts
    payload = f"{user_id_raw}:{expires_at_raw}:{nonce}"
    expected_signature = _session_signature(payload)
    if not hmac.compare_digest(signature, expected_signature):
        return None
    try:
        expires_at = int(expires_at_raw)
        user_id = int(user_id_raw)
    except ValueError:
        return None
    if expires_at < int(time.time()):
        return None
    return user_id


def get_user_id_from_session(request: Request) -> int | None:
    return parse_session_token(request.cookies.get(settings.session_cookie_name))


def attach_session_cookie(response: Response, user_id: int) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=create_session_token(user_id),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(settings.session_cookie_name)


def register_user(
    db: Session,
    *,
    email: str,
    password: str,
    display_name: str | None = None,
    workspace_name: str | None = None,
) -> User:
    user, _workspace, _membership = create_user_with_workspace(
        db,
        email=email,
        password_hash=hash_password(password),
        display_name=display_name,
        workspace_name=workspace_name,
    )
    return user


def authenticate_user(db: Session, *, email: str, password: str) -> User | None:
    normalized_email = normalize_email(email)
    user = db.query(User).filter(User.email == normalized_email, User.status == "active").first()
    if not user or not verify_password(password, user.password_hash):
        return None
    return user
