from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.services.auth import get_user_id_from_session


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    user_id = get_user_id_from_session(request)
    if user_id is None:
        return None
    return db.query(User).filter(User.id == user_id, User.status == "active").first()


def require_current_user(current_user: User | None = Depends(get_current_user)) -> User:
    if current_user is None:
        raise HTTPException(status_code=401, detail="Autenticacao obrigatoria")
    return current_user


def get_current_workspace(
    current_user: User | None = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Workspace | None:
    if current_user is None:
        return None
    membership = (
        db.query(WorkspaceMember)
        .filter(
            WorkspaceMember.user_id == current_user.id,
            WorkspaceMember.status == "active",
        )
        .order_by(WorkspaceMember.id.asc())
        .first()
    )
    if not membership:
        return None
    return (
        db.query(Workspace)
        .filter(Workspace.id == membership.workspace_id, Workspace.status == "active")
        .first()
    )


def require_current_workspace(
    current_workspace: Workspace | None = Depends(get_current_workspace),
) -> Workspace:
    if current_workspace is None:
        raise HTTPException(status_code=401, detail="Workspace autenticado obrigatorio")
    return current_workspace


__all__ = [
    "get_db",
    "get_current_user",
    "get_current_workspace",
    "require_current_user",
    "require_current_workspace",
]
