from fastapi import FastAPI
from app.core.config import settings
from app.db.database import Base, engine
from app.api.routes_jobs import router as jobs_router
from app.utils.file_manager import ensure_directories

Base.metadata.create_all(bind=engine)
ensure_directories()

app = FastAPI(title=settings.app_name, debug=settings.debug)

app.include_router(jobs_router)


@app.get("/")
def root():
    return {"message": "Video Cuts Backend is running"} 