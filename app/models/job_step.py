from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func

from app.db.database import Base


class JobStep(Base):
    __tablename__ = "job_steps"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)

    step_name = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default="pending")  # pending | running | completed | failed | skipped
    attempts = Column(Integer, nullable=False, default=0)

    error_message = Column(Text, nullable=True)
    details = Column(Text, nullable=True)

    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
