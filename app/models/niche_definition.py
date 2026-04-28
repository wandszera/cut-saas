from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func

from app.db.database import Base


class NicheDefinition(Base):
    __tablename__ = "niche_definitions"

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=True, index=True)
    name = Column(String, nullable=False)
    slug = Column(String, nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    keywords_json = Column(Text, nullable=False, default="[]")
    weights_json = Column(Text, nullable=False, default="{}")
    source = Column(String, nullable=False, default="custom")  # builtin | custom
    status = Column(String, nullable=False, default="pending")  # pending | active | archived | rejected
    llm_notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
