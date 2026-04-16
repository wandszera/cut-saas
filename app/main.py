from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.db.database import Base, engine
from app.api.routes_jobs import router as jobs_router
from app.web.routes_pages import router as pages_router

from app.models.job import Job
from app.models.clip import Clip
from app.models.candidate import Candidate
from app.models.niche_keyword import NicheKeyword
from app.models.job_step import JobStep
from app.utils.file_manager import ensure_directories

Base.metadata.create_all(bind=engine)
ensure_directories()

app = FastAPI(title=settings.app_name, debug=settings.debug)

app.include_router(jobs_router)
app.include_router(pages_router)

app.mount("/static", StaticFiles(directory=settings.base_data_dir), name="static")


@app.get("/health")
def health():
    return {"message": "ok"}
