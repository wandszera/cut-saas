from fastapi import FastAPI, Request
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.core.config import settings
from app.core.sentry import init_sentry
from app.db.database import Base, SessionLocal, engine

from app.api.jobs import router as jobs_router
from app.api.routes_files import router as files_router
from app.api.routes_billing import router as billing_router
from app.web.routes_auth import router as auth_router
from app.web.routes_billing import router as billing_pages_router
from app.web.routes_admin import router as admin_pages_router
from app.web.pages import router as pages_router

from app.models.job import Job
from app.models.clip import Clip
from app.models.candidate import Candidate
from app.models.niche_keyword import NicheKeyword
from app.models.niche_definition import NicheDefinition
from app.models.job_step import JobStep
from app.models.subscription import Subscription
from app.models.usage_event import UsageEvent
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.services.niche_registry import sync_builtin_niches
from app.services.system_diagnostics import build_runtime_readiness
from app.services.auth import get_user_id_from_session
from app.utils.file_manager import ensure_directories
from app.web.security import (
    apply_security_headers,
    attach_csrf_cookie,
    get_or_create_csrf_token,
)

if not settings.is_deployed_environment:
    Base.metadata.create_all(bind=engine)
ensure_directories()
with SessionLocal() as db:
    sync_builtin_niches(db)

# Sentry — inicializa apenas se SENTRY_DSN estiver definido no .env
try:
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration
    _sentry_integrations = [
        StarletteIntegration(transaction_style="endpoint"),
        FastApiIntegration(transaction_style="endpoint"),
    ]
except ImportError:
    _sentry_integrations = []

init_sentry(integrations=_sentry_integrations)

app = FastAPI(title=settings.app_name, debug=settings.debug)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=settings.proxy_trusted_hosts_list or "127.0.0.1")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts_list or ["localhost", "127.0.0.1", "testserver"])


@app.middleware("http")
async def apply_web_security(request: Request, call_next):
    csrf_token, token_created = get_or_create_csrf_token(request)
    request.state.csrf_token = csrf_token
    response = await call_next(request)
    apply_security_headers(request, response)
    if token_created:
        attach_csrf_cookie(response, csrf_token)
    return response


@app.middleware("http")
async def attach_authenticated_account_context(request: Request, call_next):
    request.state.current_user = None
    request.state.current_workspace = None
    request.state.current_membership = None

    user_id = get_user_id_from_session(request)
    if user_id is not None:
        db = SessionLocal()
        try:
            current_user = db.query(User).filter(User.id == user_id, User.status == "active").first()
            if current_user is not None:
                membership = (
                    db.query(WorkspaceMember)
                    .filter(
                        WorkspaceMember.user_id == current_user.id,
                        WorkspaceMember.status == "active",
                    )
                    .order_by(WorkspaceMember.id.asc())
                    .first()
                )
                workspace = None
                if membership is not None:
                    workspace = (
                        db.query(Workspace)
                        .filter(Workspace.id == membership.workspace_id, Workspace.status == "active")
                        .first()
                    )
                db.expunge_all()
                request.state.current_user = current_user
                request.state.current_membership = membership
                request.state.current_workspace = workspace
        finally:
            db.close()

    return await call_next(request)

app.include_router(auth_router)
app.include_router(files_router)
app.include_router(billing_router)
app.include_router(billing_pages_router)
app.include_router(admin_pages_router)
app.include_router(jobs_router)
app.include_router(pages_router)

app.mount("/assets", StaticFiles(directory="app/static"), name="assets")


@app.get("/health")
def health():
    return {"message": "ok"}


@app.get("/health/live")
def health_live():
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready():
    readiness = build_runtime_readiness()
    return {
        "status": "ready" if readiness["ready"] else "not_ready",
        **readiness,
    }
