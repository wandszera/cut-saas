from datetime import datetime
from pydantic import BaseModel, ConfigDict

class CandidateResponse(BaseModel):
    id: int
    job_id: int
    mode: str
    start_time: float
    end_time: float
    duration: float
    heuristic_score: float | None
    score: float
    reason: str | None
    opening_text: str | None
    closing_text: str | None
    full_text: str | None
    status: str
    is_favorite: bool
    editorial_notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
