import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.clip import Clip
from app.models.job import Job
from app.models.usage_event import UsageEvent
from app.services.exports import list_job_export_bundles
from app.services.storage import get_storage


@dataclass(frozen=True)
class StorageUsage:
    workspace_id: int
    files_count: int
    total_bytes: int


def record_usage_event(
    db: Session,
    *,
    workspace_id: int | None,
    job_id: int | None,
    event_type: str,
    quantity: float,
    unit: str,
    idempotency_key: str,
    details: dict | None = None,
) -> UsageEvent | None:
    if workspace_id is None:
        return None
    existing = (
        db.query(UsageEvent)
        .filter(UsageEvent.idempotency_key == idempotency_key)
        .first()
    )
    if existing:
        return existing

    event = UsageEvent(
        workspace_id=workspace_id,
        job_id=job_id,
        event_type=event_type,
        quantity=float(quantity),
        unit=unit,
        idempotency_key=idempotency_key,
        details=json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def record_video_processed_usage(db: Session, job: Job, *, duration_seconds: float | None) -> UsageEvent | None:
    if duration_seconds is None:
        return None
    minutes = max(0.0, round(float(duration_seconds) / 60.0, 4))
    return record_usage_event(
        db,
        workspace_id=job.workspace_id,
        job_id=job.id,
        event_type="video_processed",
        quantity=minutes,
        unit="minute",
        idempotency_key=f"job:{job.id}:video_processed",
        details={"duration_seconds": duration_seconds},
    )


def record_render_usage(db: Session, job: Job, clip: Clip) -> UsageEvent | None:
    return record_usage_event(
        db,
        workspace_id=job.workspace_id,
        job_id=job.id,
        event_type="render",
        quantity=1,
        unit="render",
        idempotency_key=f"clip:{clip.id}:render",
        details={"clip_id": clip.id, "mode": clip.mode, "duration": clip.duration},
    )


def record_llm_usage(db: Session, job: Job, *, provider: str, model: str) -> UsageEvent | None:
    return record_usage_event(
        db,
        workspace_id=job.workspace_id,
        job_id=job.id,
        event_type="llm_call",
        quantity=1,
        unit="call",
        idempotency_key=f"job:{job.id}:llm_enrichment",
        details={"provider": provider, "model": model},
    )


def record_storage_snapshot_usage(db: Session, workspace_id: int) -> UsageEvent | None:
    usage = calculate_workspace_storage_usage(db, workspace_id)
    return record_usage_event(
        db,
        workspace_id=workspace_id,
        job_id=None,
        event_type="storage_snapshot",
        quantity=usage.total_bytes,
        unit="byte",
        idempotency_key=f"workspace:{workspace_id}:storage_snapshot:{usage.total_bytes}:{usage.files_count}",
        details={"files_count": usage.files_count},
    )


def _file_size(path_value: str | None) -> int:
    path = get_storage().resolve_path(path_value)
    if not path or not path.exists() or not path.is_file():
        return 0
    return path.stat().st_size


def calculate_workspace_storage_usage(db: Session, workspace_id: int) -> StorageUsage:
    total_bytes = 0
    files_count = 0
    jobs = db.query(Job).filter(Job.workspace_id == workspace_id).all()
    job_ids = [job.id for job in jobs]
    clips = db.query(Clip).filter(Clip.job_id.in_(job_ids)).all() if job_ids else []

    paths = []
    for job in jobs:
        paths.extend([job.video_path, job.audio_path, job.transcript_path, job.result_path])
        for export in list_job_export_bundles(job.id):
            paths.append(export["path"])
    paths.extend(clip.output_path for clip in clips)

    seen = set()
    for path in paths:
        if not path or path in seen:
            continue
        seen.add(path)
        size = _file_size(path)
        if size:
            files_count += 1
            total_bytes += size

    return StorageUsage(workspace_id=workspace_id, files_count=files_count, total_bytes=total_bytes)
