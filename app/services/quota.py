from dataclasses import dataclass
from datetime import datetime, UTC, timedelta

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.subscription import Subscription
from app.models.usage_event import UsageEvent
from app.services.plans import ACTIVE_SUBSCRIPTION_STATUSES, Plan, get_workspace_plan


@dataclass(frozen=True)
class QuotaStatus:
    workspace_id: int
    plan: Plan
    used_video_minutes: float
    period_start: datetime
    period_end: datetime | None = None

    @property
    def limit_video_minutes(self) -> float:
        return self.plan.monthly_video_minutes

    @property
    def remaining_video_minutes(self) -> float:
        return max(0.0, self.limit_video_minutes - self.used_video_minutes)

    @property
    def is_exceeded(self) -> bool:
        return self.used_video_minutes >= self.limit_video_minutes

    @property
    def is_near_limit(self) -> bool:
        return self.used_video_minutes >= self.limit_video_minutes * self.plan.warning_threshold_ratio

    def to_dict(self) -> dict:
        return {
            "plan": self.plan.slug,
            "plan_name": self.plan.name,
            "used_video_minutes": round(self.used_video_minutes, 4),
            "limit_video_minutes": self.limit_video_minutes,
            "remaining_video_minutes": round(self.remaining_video_minutes, 4),
            "is_exceeded": self.is_exceeded,
            "is_near_limit": self.is_near_limit,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat() if self.period_end else None,
        }


def _month_start(now: datetime | None = None) -> datetime:
    current = now or datetime.now(UTC)
    return current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _active_subscription_period(db: Session, workspace_id: int) -> tuple[datetime, datetime] | None:
    subscription = (
        db.query(Subscription)
        .filter(Subscription.workspace_id == workspace_id)
        .order_by(Subscription.updated_at.desc(), Subscription.id.desc())
        .first()
    )
    if not subscription or subscription.status not in ACTIVE_SUBSCRIPTION_STATUSES or not subscription.current_period_end:
        return None
    period_end = subscription.current_period_end
    if period_end.tzinfo is None:
        period_end = period_end.replace(tzinfo=UTC)
    return period_end - timedelta(days=30), period_end


def get_workspace_quota_status(db: Session, workspace_id: int) -> QuotaStatus:
    plan = get_workspace_plan(db, workspace_id)
    period = _active_subscription_period(db, workspace_id)
    period_start, period_end = period if period else (_month_start(), None)
    filters = [
        UsageEvent.workspace_id == workspace_id,
        UsageEvent.event_type == "video_processed",
        UsageEvent.created_at >= period_start,
    ]
    if period_end is not None:
        filters.append(UsageEvent.created_at < period_end)
    used = db.query(UsageEvent).filter(*filters).all()
    used_minutes = sum(float(event.quantity or 0) for event in used)
    return QuotaStatus(
        workspace_id=workspace_id,
        plan=plan,
        used_video_minutes=used_minutes,
        period_start=period_start,
        period_end=period_end,
    )


def ensure_workspace_can_start_job(db: Session, workspace_id: int) -> QuotaStatus:
    status = get_workspace_quota_status(db, workspace_id)
    if status.is_exceeded:
        raise HTTPException(
            status_code=402,
            detail=(
                "Limite mensal de processamento atingido. "
                "Voce ainda pode acessar e baixar arquivos ja gerados."
            ),
        )
    return status
