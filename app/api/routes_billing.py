import json
import httpx

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import require_current_workspace, require_workspace_owner
from app.db.database import get_db
from app.models.workspace import Workspace
from app.services.billing import (
    activate_checkout_session,
    apply_billing_webhook,
    build_billing_overview,
    cancel_current_subscription,
    create_checkout_session,
    serialize_subscription,
    verify_billing_webhook_signature,
    create_customer_portal,
)
from app.web.security import validate_csrf_request
from app.services.rate_limit import BILLING_MUTATION_RULE, BILLING_WEBHOOK_RULE, enforce_rate_limit


router = APIRouter(prefix="/api/billing", tags=["billing"])


@router.get("/status")
def get_billing_status(
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    return build_billing_overview(db, workspace.id)


@router.post("/checkout", dependencies=[Depends(validate_csrf_request)])
def create_billing_checkout(
    request: Request,
    plan: str,
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_workspace_owner),
):
    enforce_rate_limit(request, BILLING_MUTATION_RULE, suffix=f"{workspace.id}:api-checkout")
    try:
        session = create_checkout_session(db, workspace_id=workspace.id, plan_slug=plan)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail="Pagamento indisponivel. Tente novamente em instantes.") from exc
    return {
        "checkout_id": session.checkout_id,
        "checkout_url": session.checkout_url,
        "plan": session.plan_slug,
        "provider": session.provider,
    }


@router.post("/checkout/{checkout_id}/complete", dependencies=[Depends(validate_csrf_request)])
def complete_billing_checkout(
    request: Request,
    checkout_id: str,
    db: Session = Depends(get_db),
    _workspace: Workspace = Depends(require_workspace_owner),
):
    enforce_rate_limit(request, BILLING_MUTATION_RULE, suffix=f"complete:{checkout_id}")
    try:
        subscription = activate_checkout_session(db, checkout_id, workspace_id=_workspace.id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return serialize_subscription(subscription)


@router.post("/cancel", dependencies=[Depends(validate_csrf_request)])
def cancel_billing_subscription(
    request: Request,
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_workspace_owner),
):
    enforce_rate_limit(request, BILLING_MUTATION_RULE, suffix=f"{workspace.id}:api-cancel")
    try:
        subscription = cancel_current_subscription(db, workspace.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Nao foi possivel cancelar. Tente novamente.") from exc
    return serialize_subscription(subscription)


@router.post("/portal", dependencies=[Depends(validate_csrf_request)])
def billing_portal(request: Request, db: Session = Depends(get_db), workspace: Workspace = Depends(require_workspace_owner)):
    enforce_rate_limit(request, BILLING_MUTATION_RULE, suffix=f"{workspace.id}:portal")
    try:
        return {"url": create_customer_portal(db, workspace.id)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Portal de pagamento temporariamente indisponivel") from exc


@router.post("/webhook")
async def billing_webhook(request: Request, db: Session = Depends(get_db)):
    enforce_rate_limit(request, BILLING_WEBHOOK_RULE)
    raw_body = await request.body()
    try:
        verify_billing_webhook_signature(raw_body, dict(request.headers))
        payload = json.loads(raw_body)
        subscription = apply_billing_webhook(db, payload)
    except (ValueError, json.JSONDecodeError) as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Falha temporaria na consulta ao provedor") from exc
    return {
        "received": True,
        "subscription": serialize_subscription(subscription) if subscription else None,
    }
