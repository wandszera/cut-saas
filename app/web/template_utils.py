from fastapi.requests import Request
from fastapi.templating import Jinja2Templates


def _template_context(request: Request) -> dict:
    membership = getattr(request.state, "current_membership", None)
    return {
        "app_current_user": getattr(request.state, "current_user", None),
        "app_current_workspace": getattr(request.state, "current_workspace", None),
        "app_current_membership": membership,
    }


def build_templates() -> Jinja2Templates:
    return Jinja2Templates(directory="app/templates", context_processors=[_template_context])
