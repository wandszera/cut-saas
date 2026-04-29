from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.database import get_db
from app.models.user import User
from app.services.auth import (
    attach_session_cookie,
    authenticate_user,
    clear_session_cookie,
    register_user,
)
from app.web.template_utils import build_templates
from app.web.security import validate_csrf_request
from app.services.rate_limit import AUTH_LOGIN_RULE, AUTH_REGISTER_RULE, enforce_rate_limit


router = APIRouter(tags=["auth"], dependencies=[Depends(validate_csrf_request)])
templates = build_templates()


def _auth_url(path: str, message: str | None = None, level: str = "info") -> str:
    if not message:
        return path
    return f"{path}?{urlencode({'message': message, 'level': level})}"


def _billing_activation_url() -> str:
    return (
        "/billing?"
        + urlencode(
            {
                "message": "Conta criada. Voce pode testar 1 video de ate 30 minutos sem cartao. Depois disso, ative o billing para continuar.",
                "level": "success",
            }
        )
    )


@router.get("/login")
def login_page(
    request: Request,
    message: str | None = None,
    level: str = "info",
    current_user: User | None = Depends(get_current_user),
):
    if current_user:
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"flash": {"message": message, "level": level} if message else None},
    )


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    enforce_rate_limit(request, AUTH_LOGIN_RULE, suffix=(email or "").strip().lower())
    user = authenticate_user(db, email=email, password=password)
    if not user:
        return RedirectResponse(
            url=_auth_url("/login", "Email ou senha invalidos.", "error"),
            status_code=303,
        )
    response = RedirectResponse(url="/dashboard", status_code=303)
    attach_session_cookie(response, user.id)
    return response


@router.get("/register")
def register_page(
    request: Request,
    message: str | None = None,
    level: str = "info",
    current_user: User | None = Depends(get_current_user),
):
    if current_user:
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(
        request,
        "register.html",
        {"flash": {"message": message, "level": level} if message else None},
    )


@router.post("/register")
def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(""),
    workspace_name: str = Form(""),
    db: Session = Depends(get_db),
):
    enforce_rate_limit(request, AUTH_REGISTER_RULE, suffix=(email or "").strip().lower())
    try:
        user = register_user(
            db,
            email=email,
            password=password,
            display_name=display_name,
            workspace_name=workspace_name,
        )
    except ValueError as exc:
        return RedirectResponse(
            url=_auth_url("/register", str(exc), "error"),
            status_code=303,
        )
    response = RedirectResponse(url=_billing_activation_url(), status_code=303)
    attach_session_cookie(response, user.id)
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(response)
    return response
