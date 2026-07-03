from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.subscription import Subscription


@dataclass(frozen=True)
class Plan:
    slug: str
    name: str
    monthly_video_minutes: float
    monthly_price_cents: int = 0
    warning_threshold_ratio: float = 0.8
    storage_limit_bytes: int = 1 * 1024 * 1024 * 1024  # 1 GB default
    max_workspaces: int = 1
    llm_enabled: bool = False
    priority_queue: bool = False


PLANS = {
    "free": Plan(
        slug="free",
        name="Free",
        monthly_video_minutes=60.0,
        storage_limit_bytes=1 * 1024 * 1024 * 1024,
        max_workspaces=1,
        llm_enabled=False,
        priority_queue=False,
    ),
    "starter": Plan(
        slug="starter",
        name="Starter",
        monthly_video_minutes=600.0,
        monthly_price_cents=4900,
        storage_limit_bytes=10 * 1024 * 1024 * 1024,  # 10 GB
        max_workspaces=1,
        llm_enabled=True,
        priority_queue=False,
    ),
    "pro": Plan(
        slug="pro",
        name="Pro",
        monthly_video_minutes=3000.0,
        monthly_price_cents=14900,
        storage_limit_bytes=50 * 1024 * 1024 * 1024,  # 50 GB
        max_workspaces=3,
        llm_enabled=True,
        priority_queue=True,
    ),
}


ACTIVE_SUBSCRIPTION_STATUSES = {"active", "trialing"}


def list_plans() -> list[Plan]:
    return list(PLANS.values())


from datetime import datetime, UTC

def get_plan(plan_slug: str | None) -> Plan:
    return PLANS.get((plan_slug or "free").strip().lower(), PLANS["free"])


def get_workspace_plan(db: Session | None = None, workspace_id: int | None = None) -> Plan:
    if db is not None and workspace_id is not None:
        subscription = (
            db.query(Subscription)
            .filter(Subscription.workspace_id == workspace_id)
            .order_by(Subscription.updated_at.desc(), Subscription.id.desc())
            .first()
        )
        if subscription and subscription.status in ACTIVE_SUBSCRIPTION_STATUSES:
            # Lógica de expiração de trial e datas recorrentes
            if subscription.current_period_end:
                period_end = subscription.current_period_end
                # Tornar a comparação segura com ou sem timezone
                now = datetime.now(UTC)
                if period_end.tzinfo is None:
                    now = now.replace(tzinfo=None)
                
                if period_end < now:
                    # Expirou! Atualizamos o status e persistimos
                    subscription.status = "canceled"
                    db.commit()
                    db.refresh(subscription)
                    return PLANS["free"]
            
            return get_plan(subscription.plan_slug)
    return PLANS["free"]
