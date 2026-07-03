from fastapi import APIRouter
from app.api.jobs.core import router as core_router
from app.api.jobs.system import router as system_router
from app.api.jobs.candidates import router as candidates_router
from app.api.jobs.clips import router as clips_router
from app.api.jobs.feedback import router as feedback_router

router = APIRouter(prefix="/jobs")

router.include_router(system_router)
router.include_router(core_router)
router.include_router(candidates_router)
router.include_router(clips_router)
router.include_router(feedback_router)
