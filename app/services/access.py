from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.job import Job
from app.services.billing import workspace_has_billing_access


TRIAL_MAX_VIDEO_MINUTES = 30
TRIAL_MAX_VIDEO_SECONDS = TRIAL_MAX_VIDEO_MINUTES * 60


def workspace_has_used_trial(db: Session, workspace_id: int) -> bool:
    return (
        db.query(Job.id)
        .filter(Job.workspace_id == workspace_id)
        .first()
        is not None
    )


def ensure_workspace_can_create_job(
    db: Session,
    workspace_id: int,
    *,
    duration_seconds: float | None = None,
) -> None:
    if workspace_has_billing_access(db, workspace_id):
        return None

    if workspace_has_used_trial(db, workspace_id):
        raise HTTPException(
            status_code=402,
            detail=(
                "Seu teste gratis ja foi usado. "
                "Cadastre um cartao para continuar criando novos videos."
            ),
        )

    if duration_seconds is None:
        raise HTTPException(
            status_code=400,
            detail="Nao foi possivel validar a duracao do video de teste.",
        )

    if duration_seconds > TRIAL_MAX_VIDEO_SECONDS:
        raise HTTPException(
            status_code=402,
            detail=(
                "No teste gratis voce pode processar 1 video de ate 30 minutos. "
                "Cadastre um cartao para enviar videos maiores."
            ),
        )

    return None
