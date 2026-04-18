from sqlalchemy import Boolean, Column, Integer, String, Float, Text, DateTime, ForeignKey
from sqlalchemy.sql import func

from app.db.database import Base


class Candidate(Base):
    __tablename__ = "candidates"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)

    mode = Column(String, nullable=False)  # short | long

    start_time = Column(Float, nullable=False)
    end_time = Column(Float, nullable=False)
    duration = Column(Float, nullable=False)

    heuristic_score = Column(Float, nullable=True)
    score = Column(Float, nullable=False, default=0.0)
    reason = Column(Text, nullable=True)

    opening_text = Column(Text, nullable=True)
    closing_text = Column(Text, nullable=True)
    full_text = Column(Text, nullable=True)

    hook_score = Column(Float, nullable=True)
    clarity_score = Column(Float, nullable=True)
    closure_score = Column(Float, nullable=True)
    emotion_score = Column(Float, nullable=True)
    duration_fit_score = Column(Float, nullable=True)
    transcript_context_score = Column(Float, nullable=True)
    llm_score = Column(Float, nullable=True)
    llm_why = Column(Text, nullable=True)
    llm_title = Column(Text, nullable=True)
    llm_hook = Column(Text, nullable=True)

    status = Column(String, nullable=False, default="pending")  # pending, approved, rejected, rendered
    is_favorite = Column(Boolean, nullable=False, default=False)
    editorial_notes = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
