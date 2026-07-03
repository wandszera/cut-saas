from fastapi import APIRouter, Depends, HTTPException
from starlette.requests import Request
from sqlalchemy.orm import Session

from app.api.deps import require_admin_user, get_db
from app.models.user import User
from app.services.admin_metrics import calculate_admin_financial_metrics, list_workspaces_usage_reports
from app.web.template_utils import build_templates

router = APIRouter(tags=["admin-pages"])
templates = build_templates()


@router.get("/admin")
def admin_dashboard_page(
    request: Request,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin_user),
):
    """Renderiza a página principal do dashboard administrativo."""
    metrics = calculate_admin_financial_metrics(db)
    reports = list_workspaces_usage_reports(db)
    
    return templates.TemplateResponse(
        request,
        "admin_dashboard.html",
        {
            "metrics": metrics,
            "reports": reports,
        },
    )
