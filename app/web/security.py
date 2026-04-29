import secrets

from fastapi import HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

from app.core.config import settings


CSRF_COOKIE_NAME = "cut_saas_csrf"
CSRF_FORM_FIELD = "csrf_token"
CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def get_or_create_csrf_token(request: Request) -> tuple[str, bool]:
    existing = request.cookies.get(CSRF_COOKIE_NAME)
    if existing:
        return existing, False
    return secrets.token_urlsafe(32), True


def attach_csrf_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )


async def validate_csrf_request(request: Request) -> None:
    if request.method.upper() in CSRF_SAFE_METHODS:
        return
    if request.url.path.startswith("/api/"):
        return

    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    if not cookie_token:
        raise HTTPException(status_code=403, detail="CSRF token ausente")

    submitted_token = request.headers.get("X-CSRF-Token")
    if not submitted_token:
        content_type = request.headers.get("content-type", "").lower()
        if content_type.startswith("application/x-www-form-urlencoded") or content_type.startswith("multipart/form-data"):
            form = await request.form()
            submitted_token = str(form.get(CSRF_FORM_FIELD) or "")

    if not submitted_token or not secrets.compare_digest(submitted_token, cookie_token):
        raise HTTPException(status_code=403, detail="CSRF token invalido")


def build_content_security_policy() -> str:
    return "; ".join(
        (
            "default-src 'self'",
            "base-uri 'self'",
            "object-src 'none'",
            "frame-ancestors 'none'",
            "form-action 'self'",
            "script-src 'self' 'unsafe-inline'",
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
            "font-src 'self' https://fonts.gstatic.com data:",
            "img-src 'self' data: blob:",
            "media-src 'self' blob:",
            "connect-src 'self'",
        )
    )


def apply_security_headers(request: Request, response: Response) -> None:
    response.headers["Content-Security-Policy"] = build_content_security_policy()
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if settings.is_deployed_environment:
        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        is_secure_request = request.url.scheme == "https" or forwarded_proto.lower() == "https"
        if is_secure_request:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"


def build_csrf_error_response(detail: str) -> Response:
    return PlainTextResponse(detail, status_code=403)
