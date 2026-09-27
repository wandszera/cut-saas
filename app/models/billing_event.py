from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.sql import func

from app.db.database import Base


class BillingEvent(Base):
    """Minimal receipt ledger; never persist payment payloads or card information."""
    __tablename__ = "billing_events"
    __table_args__ = (UniqueConstraint("provider", "event_id", name="uq_billing_event_provider_id"),)

    id = Column(Integer, primary_key=True)
    provider = Column(String, nullable=False)
    event_id = Column(String, nullable=False)
    event_type = Column(String, nullable=False)
    subscription_id = Column(Integer, ForeignKey("subscriptions.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
