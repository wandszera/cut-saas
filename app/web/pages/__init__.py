
from fastapi import APIRouter
from app.web.pages.dashboard import router as dashboard_router
from app.web.pages.job_detail import router as job_detail_router
from app.web.pages.actions import router as actions_router

router = APIRouter(include_in_schema=False)

router.include_router(dashboard_router)
router.include_router(job_detail_router)
router.include_router(actions_router)
