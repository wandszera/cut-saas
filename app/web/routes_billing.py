from urllib.parse import urlencode
import httpx

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from starlette.requests import Request
from sqlalchemy.orm import Session

from app.api.deps import require_current_workspace, require_workspace_owner
from app.core.config import settings
from app.models.subscription import Subscription
from app.db.database import get_db
from app.models.workspace import Workspace
from app.services.billing import (
    activate_checkout_session,
    build_billing_overview,
    cancel_current_subscription,
    create_checkout_session,
    create_customer_portal,
)
from app.services.rate_limit import BILLING_MUTATION_RULE, enforce_rate_limit
from app.web.security import validate_csrf_request
from app.web.template_utils import build_templates


router = APIRouter(tags=["billing-pages"], dependencies=[Depends(validate_csrf_request)])
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
    request: Request,
    plan: str = Form(...),
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_workspace_owner),
):
    enforce_rate_limit(request, BILLING_MUTATION_RULE, suffix=f"{workspace.id}:checkout")
    try:
        session = create_checkout_session(db, workspace_id=workspace.id, plan_slug=plan)
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(url=_billing_url(str(exc), "warning"), status_code=303)
    except httpx.HTTPError:
        db.rollback()
        return RedirectResponse(url=_billing_url("Pagamento temporariamente indisponivel. Tente novamente.", "warning"), status_code=303)
    return RedirectResponse(url=session.checkout_url, status_code=303)


@router.get("/billing/checkout/complete")
def complete_checkout_from_page(
    request: Request,
    session_id: str,
    success_url: str = "/billing",
    cancel_url: str = "/billing",
    db: Session = Depends(get_db),
    _workspace: Workspace = Depends(require_workspace_owner),
):
    if settings.billing_provider != "mock" or settings.is_deployed_environment:
        raise HTTPException(status_code=404, detail="Checkout simulado indisponivel")
    subscription = db.query(Subscription).filter_by(provider_checkout_id=session_id, workspace_id=_workspace.id, provider="mock").first()
    if not subscription:
        raise HTTPException(status_code=404, detail="Checkout nao encontrado")
    return templates.TemplateResponse(request, "mock_checkout.html", {"session_id": session_id})


@router.post("/billing/checkout/complete")
def activate_mock_checkout(session_id: str = Form(...), db: Session = Depends(get_db), workspace: Workspace = Depends(require_workspace_owner)):
    try:
        activate_checkout_session(db, session_id, workspace_id=workspace.id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(
        url=_billing_url("Assinatura simulada ativada."),
        status_code=303,
    )


@router.post("/billing/cancel")
def cancel_subscription_from_page(
    request: Request,
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_workspace_owner),
):
    enforce_rate_limit(request, BILLING_MUTATION_RULE, suffix=f"{workspace.id}:cancel")
    try:
        cancel_current_subscription(db, workspace.id)
    except ValueError as exc:
        return RedirectResponse(url=_billing_url(str(exc), "warning"), status_code=303)
    except httpx.HTTPError:
        return RedirectResponse(url=_billing_url("Nao foi possivel cancelar. Tente novamente.", "warning"), status_code=303)
    return RedirectResponse(
        url=_billing_url("Assinatura cancelada. O workspace voltou para o plano Free.", "success"),
        status_code=303,
    )


@router.post("/billing/portal")
def portal_from_page(request: Request, db: Session = Depends(get_db), workspace: Workspace = Depends(require_workspace_owner)):
    enforce_rate_limit(request, BILLING_MUTATION_RULE, suffix=f"{workspace.id}:portal")
    try:
        return RedirectResponse(create_customer_portal(db, workspace.id), status_code=303)
    except ValueError as exc:
        return RedirectResponse(_billing_url(str(exc), "warning"), status_code=303)
    except httpx.HTTPError:
        return RedirectResponse(_billing_url("Portal temporariamente indisponivel. Tente novamente.", "warning"), status_code=303)
