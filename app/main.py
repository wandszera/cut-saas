from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.db.database import Base, SessionLocal, engine
from app.db.migrations import (
    ensure_candidate_editorial_columns,
    ensure_clip_editorial_columns,
    ensure_job_insights_columns,
    ensure_niche_definition_columns,
)
from app.api.routes_jobs import router as jobs_router
from app.web.routes_pages import router as pages_router

from app.models.job import Job
from app.models.clip import Clip
from app.models.candidate import Candidate
from app.models.niche_keyword import NicheKeyword
from app.models.niche_definition import NicheDefinition
from app.models.job_step import JobStep
from app.services.niche_registry import sync_builtin_niches
from app.utils.file_manager import ensure_directories

Base.metadata.create_all(bind=engine)
ensure_job_insights_columns()
ensure_candidate_editorial_columns()
ensure_clip_editorial_columns()
ensure_niche_definition_columns()
ensure_directories()
with SessionLocal() as db:
    sync_builtin_niches(db)

app = FastAPI(title=settings.app_name, debug=settings.debug)

app.include_router(jobs_router)
app.include_router(pages_router)

app.mount("/static", StaticFiles(directory=settings.base_data_dir), name="static")


@app.get("/health")
def health():
    return {"message": "ok"}
