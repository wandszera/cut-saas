from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from starlette.requests import Request
from sqlalchemy.orm import Session

from app.api.deps import require_current_workspace
from app.db.database import get_db
from app.models.workspace import Workspace
from app.services.billing import (
    activate_checkout_session,
    build_billing_overview,
    cancel_current_subscription,
    create_checkout_session,
)
from app.web.template_utils import build_templates


router = APIRouter(tags=["billing-pages"])
templates = build_templates()


def _billing_url(message: str | None = None, level: str = "success") -> str:
    params: dict[str, str] = {}
    if message:
        params["message"] = message
        params["level"] = level
    query = urlencode(params)
    return f"/billing?{query}" if query else "/billing"


@router.get("/billing")
def billing_page(
    request: Request,
    message: str | None = None,
    level: str = "success",
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    overview = build_billing_overview(db, workspace.id)
    return templates.TemplateResponse(
        request,
        "billing.html",
        {
            "plans": overview["plans"],
            "subscription": overview["subscription"],
            "billing_provider": overview["provider"],
            "billing_provider_ready": overview["provider_ready"],
            "billing_activation_required": overview["billing_activation_required"],
            "quota_status": overview["quota"],
            "flash": {"message": message, "level": level} if message else None,
        },
    )


@router.post("/billing/checkout")
def start_checkout_from_page(
    plan: str = Form(...),
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    try:
        session = create_checkout_session(db, workspace_id=workspace.id, plan_slug=plan)
    except ValueError as exc:
        return RedirectResponse(url=_billing_url(str(exc), "warning"), status_code=303)
    return RedirectResponse(url=session.checkout_url, status_code=303)


@router.get("/billing/checkout/complete")
def complete_checkout_from_page(
    session_id: str,
    success_url: str = "/billing",
    cancel_url: str = "/billing",
    db: Session = Depends(get_db),
    _workspace: Workspace = Depends(require_current_workspace),
):
    try:
        activate_checkout_session(db, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    separator = "&" if "?" in success_url else "?"
    return RedirectResponse(
        url=f"{success_url}{separator}{urlencode({'message': 'Assinatura ativada.', 'level': 'success'})}",
        status_code=303,
    )


@router.post("/billing/cancel")
def cancel_subscription_from_page(
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    try:
        cancel_current_subscription(db, workspace.id)
    except ValueError as exc:
        return RedirectResponse(url=_billing_url(str(exc), "warning"), status_code=303)
    return RedirectResponse(
        url=_billing_url("Assinatura cancelada. O workspace voltou para o plano Free.", "success"),
        status_code=303,
    )
