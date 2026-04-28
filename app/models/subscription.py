from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.sql import func

from app.db.database import Base


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    provider = Column(String, nullable=False, default="mock")
    provider_customer_id = Column(String, nullable=True, index=True)
    provider_subscription_id = Column(String, nullable=True, index=True)
    provider_checkout_id = Column(String, nullable=True, unique=True, index=True)
    plan_slug = Column(String, nullable=False, default="free", index=True)
    status = Column(String, nullable=False, default="inactive", index=True)
    current_period_end = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
