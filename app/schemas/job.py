from pydantic import BaseModel, HttpUrl, Field
from typing import Optional
from datetime import datetime


class JobCreateYouTube(BaseModel):
    url: HttpUrl


class JobCreateLocalVideo(BaseModel):
    video_path: str
    title: Optional[str] = None


class AnalyzeRequest(BaseModel):
    mode: str = "short"
    top_n: int = 10


class RenderRequest(BaseModel):
    top_n: int = 5
    burn_subtitles: bool = False
    mode: str = "short"
    render_preset: str = "clean"


class RenderCandidateRequest(BaseModel):
    candidate_index: int = Field(..., ge=0)
    burn_subtitles: bool = False
    mode: str = "short"
    render_preset: str = "clean"


class ManualRenderRequest(BaseModel):
    start: float | str
    end: float | str
    burn_subtitles: bool = False
    mode: str = "short"
    render_preset: str = "clean"


class CandidateNotesRequest(BaseModel):
    editorial_notes: str = ""


class NicheCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None


class JobResponse(BaseModel):
    id: int
    source_type: str
    source_value: str
    status: str
    title: Optional[str] = None
    video_path: Optional[str] = None
    audio_path: Optional[str] = None
    transcript_path: Optional[str] = None
    result_path: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True
