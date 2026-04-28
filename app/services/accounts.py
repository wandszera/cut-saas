import re

from sqlalchemy.orm import Session

from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def slugify_workspace_name(name: str) -> str:
    normalized = (name or "").strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized)
    normalized = normalized.strip("-")
    return normalized or "workspace"


def build_unique_workspace_slug(db: Session, name: str) -> str:
    base_slug = slugify_workspace_name(name)
    slug = base_slug
    suffix = 2
    while db.query(Workspace).filter(Workspace.slug == slug).first():
        slug = f"{base_slug}-{suffix}"
        suffix += 1
    return slug


def create_user_with_workspace(
    db: Session,
    *,
    email: str,
    password_hash: str,
    display_name: str | None = None,
    workspace_name: str | None = None,
) -> tuple[User, Workspace, WorkspaceMember]:
    normalized_email = normalize_email(email)
    if not normalized_email:
        raise ValueError("email e obrigatorio")
    if not password_hash:
        raise ValueError("password_hash e obrigatorio")

    existing = db.query(User).filter(User.email == normalized_email).first()
    if existing:
        raise ValueError("email ja cadastrado")

    resolved_workspace_name = (workspace_name or display_name or normalized_email.split("@")[0]).strip()
    slug = build_unique_workspace_slug(db, resolved_workspace_name)

    user = User(
        email=normalized_email,
        password_hash=password_hash,
        display_name=(display_name or "").strip() or None,
    )
    db.add(user)
    db.flush()

    workspace = Workspace(
        name=resolved_workspace_name,
        slug=slug,
        owner_user_id=user.id,
    )
    db.add(workspace)
    db.flush()

    membership = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=user.id,
        role="owner",
        status="active",
    )
    db.add(membership)
    db.commit()
    db.refresh(user)
    db.refresh(workspace)
    db.refresh(membership)
    return user, workspace, membership
