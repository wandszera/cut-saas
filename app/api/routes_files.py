from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import require_current_workspace
from app.db.database import get_db
from app.models.clip import Clip
from app.models.job import Job
from app.models.workspace import Workspace
from app.services.exports import list_job_export_bundles
from app.services.storage import get_storage
from app.utils.media_urls import parse_media_access_token


router = APIRouter(prefix="/files", tags=["files"])


def _job_file_keys(job: Job) -> set[str]:
    storage = get_storage()
    keys = set()
    for path in (job.video_path, job.audio_path, job.transcript_path, job.result_path):
        if not path:
            continue
        key = storage.key_for_path(path)
        if key:
            keys.add(key)
    return keys


def _clip_file_keys(clips: list[Clip]) -> set[str]:
    storage = get_storage()
    keys = set()
    for clip in clips:
        key = storage.key_for_path(clip.output_path)
        if key:
            keys.add(key)
    return keys


def _export_file_keys(job_id: int) -> set[str]:
    storage = get_storage()
    keys = set()
    for row in list_job_export_bundles(job_id):
        key = storage.key_for_path(row["path"])
        if key:
            keys.add(key)
    return keys


def _workspace_can_access_key(db: Session, workspace: Workspace, key: str) -> bool:
    jobs = db.query(Job).filter(Job.workspace_id == workspace.id).all()
    job_ids = [job.id for job in jobs]
    clips = db.query(Clip).filter(Clip.job_id.in_(job_ids)).all() if job_ids else []

    allowed_keys = set()
    for job in jobs:
        allowed_keys.update(_job_file_keys(job))
        allowed_keys.update(_export_file_keys(job.id))
    allowed_keys.update(_clip_file_keys(clips))
    return key in allowed_keys


@router.get("/download/{token}")
def download_signed_file(
    token: str,
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    key = parse_media_access_token(token)
    if not key:
        raise HTTPException(status_code=404, detail="Arquivo nao encontrado")
    if not _workspace_can_access_key(db, workspace, key):
        raise HTTPException(status_code=404, detail="Arquivo nao encontrado")

    path = get_storage().resolve_path(key)
    if not path or not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo nao encontrado")

    return FileResponse(path=str(path), filename=Path(path).name)
