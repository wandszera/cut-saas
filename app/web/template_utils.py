from fastapi.requests import Request
from fastapi.templating import Jinja2Templates

from app.web.security import CSRF_FORM_FIELD


def _template_context(request: Request) -> dict:
    membership = getattr(request.state, "current_membership", None)
    return {
        "app_current_user": getattr(request.state, "current_user", None),
        "app_current_workspace": getattr(request.state, "current_workspace", None),
        "app_current_membership": membership,
        "csrf_token": getattr(request.state, "csrf_token", request.cookies.get("cut_saas_csrf", "")),
    }


def build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory="app/templates", context_processors=[_template_context])
    templates.env.globals["csrf_form_field"] = CSRF_FORM_FIELD
    return templates
