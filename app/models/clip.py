from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey
from sqlalchemy.sql import func

from app.db.database import Base


class Clip(Base):
    __tablename__ = "clips"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)

    source = Column(String, nullable=False)  # candidate | manual
    mode = Column(String, nullable=False)    # short | long

    start_time = Column(Float, nullable=False)
    end_time = Column(Float, nullable=False)
    duration = Column(Float, nullable=False)

    score = Column(Float, nullable=True)
    reason = Column(String, nullable=True)
    text = Column(String, nullable=True)

    subtitles_burned = Column(Boolean, default=False, nullable=False)
    output_path = Column(String, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())