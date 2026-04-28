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


PLANS = {
    "free": Plan(slug="free", name="Free", monthly_video_minutes=60.0),
    "starter": Plan(slug="starter", name="Starter", monthly_video_minutes=600.0, monthly_price_cents=4900),
}


ACTIVE_SUBSCRIPTION_STATUSES = {"active", "trialing"}


def list_plans() -> list[Plan]:
    return list(PLANS.values())


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
            return get_plan(subscription.plan_slug)
    return PLANS["free"]
