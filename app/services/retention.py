from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.clip import Clip
from app.models.job import Job
from app.services.exports import list_job_export_bundles
from app.services.storage import get_storage


PRESERVED_PUBLICATION_STATUSES = {"ready", "published"}


@dataclass(frozen=True)
class RetentionPolicy:
    retention_days: int
    preserve_approved_artifacts: bool = True

    @property
    def cutoff(self) -> datetime:
        return datetime.now(UTC) - timedelta(days=max(0, self.retention_days))


@dataclass
class RetentionReport:
    workspace_id: int | None
    cutoff: datetime
    deleted: list[dict] = field(default_factory=list)
    preserved: list[dict] = field(default_factory=list)

    @property
    def deleted_count(self) -> int:
        return len(self.deleted)

    @property
    def deleted_bytes(self) -> int:
        return sum(int(item.get("size_bytes") or 0) for item in self.deleted)


def default_retention_policy() -> RetentionPolicy:
    return RetentionPolicy(
        retention_days=settings.artifact_retention_days,
        preserve_approved_artifacts=settings.preserve_approved_artifacts,
    )


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _is_expired(job: Job, policy: RetentionPolicy) -> bool:
    updated_at = _as_aware(job.updated_at) or _as_aware(job.created_at)
    return bool(updated_at and updated_at < policy.cutoff)


def _delete_file(path_value: str | None, *, reason: str) -> dict | None:
    storage = get_storage()
    path = storage.resolve_path(path_value)
    if not path or not path.exists() or not path.is_file():
        return None
    size = path.stat().st_size
    key = storage.key_for_path(path) or str(path)
    storage.delete(str(path))
    return {"key": key, "path": str(path), "size_bytes": size, "reason": reason}


def cleanup_expired_workspace_artifacts(
    db: Session,
    workspace_id: int,
    *,
    policy: RetentionPolicy | None = None,
) -> RetentionReport:
    resolved_policy = policy or default_retention_policy()
    report = RetentionReport(workspace_id=workspace_id, cutoff=resolved_policy.cutoff)
    jobs = db.query(Job).filter(Job.workspace_id == workspace_id).all()

    for job in jobs:
        if not _is_expired(job, resolved_policy):
            continue

        clips = db.query(Clip).filter(Clip.job_id == job.id).all()
        preserved_clip_paths = set()
        for clip in clips:
            should_preserve = (
                resolved_policy.preserve_approved_artifacts
                and clip.publication_status in PRESERVED_PUBLICATION_STATUSES
            )
            if should_preserve:
                preserved_clip_paths.add(clip.output_path)
                report.preserved.append(
                    {
                        "job_id": job.id,
                        "clip_id": clip.id,
                        "path": clip.output_path,
                        "reason": f"publication_status:{clip.publication_status}",
                    }
                )
                continue
            deleted = _delete_file(clip.output_path, reason="expired_clip")
            if deleted:
                deleted.update({"job_id": job.id, "clip_id": clip.id})
                report.deleted.append(deleted)

        for path_value, reason in [
            (job.video_path, "expired_job_video"),
            (job.audio_path, "expired_job_audio"),
            (job.transcript_path, "expired_job_transcript"),
            (job.result_path, "expired_job_result"),
        ]:
            if path_value in preserved_clip_paths:
                continue
            deleted = _delete_file(path_value, reason=reason)
            if deleted:
                deleted["job_id"] = job.id
                report.deleted.append(deleted)

        for export in list_job_export_bundles(job.id):
            deleted = _delete_file(export["path"], reason="expired_export")
            if deleted:
                deleted["job_id"] = job.id
                report.deleted.append(deleted)

    return report


def cleanup_expired_artifacts(db: Session, *, policy: RetentionPolicy | None = None) -> list[RetentionReport]:
    workspace_ids = [
        row[0]
        for row in db.query(Job.workspace_id).filter(Job.workspace_id.is_not(None)).distinct().all()
    ]
    return [
        cleanup_expired_workspace_artifacts(db, workspace_id, policy=policy)
        for workspace_id in workspace_ids
    ]
