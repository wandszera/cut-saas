import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import require_current_workspace
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
)


router = APIRouter(prefix="/api/billing", tags=["billing"])


@router.get("/status")
def get_billing_status(
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    return build_billing_overview(db, workspace.id)


@router.post("/checkout")
def create_billing_checkout(
    plan: str,
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    try:
        session = create_checkout_session(db, workspace_id=workspace.id, plan_slug=plan)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "checkout_id": session.checkout_id,
        "checkout_url": session.checkout_url,
        "plan": session.plan_slug,
        "provider": session.provider,
    }


@router.post("/checkout/{checkout_id}/complete")
def complete_billing_checkout(
    checkout_id: str,
    db: Session = Depends(get_db),
    _workspace: Workspace = Depends(require_current_workspace),
):
    try:
        subscription = activate_checkout_session(db, checkout_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return serialize_subscription(subscription)


@router.post("/cancel")
def cancel_billing_subscription(
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    try:
        subscription = cancel_current_subscription(db, workspace.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return serialize_subscription(subscription)


@router.post("/webhook")
async def billing_webhook(request: Request, db: Session = Depends(get_db)):
    raw_body = await request.body()
    try:
        verify_billing_webhook_signature(raw_body, dict(request.headers))
        payload = json.loads(raw_body)
        subscription = apply_billing_webhook(db, payload)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "received": True,
        "subscription": serialize_subscription(subscription),
    }
