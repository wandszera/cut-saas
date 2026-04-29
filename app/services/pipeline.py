import json
import logging
import os
import socket
from datetime import datetime, timedelta, UTC
from typing import Any
from uuid import uuid4

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.database import SessionLocal
from app.models.job import Job
from app.models.job_step import JobStep
from app.models.candidate import Candidate
from app.services.audio import extract_audio_from_video
from app.services.candidates import ensure_default_candidates_for_job
from app.services.niche_classifier import detect_niche
from app.services.segmentation import load_transcript
from app.services.transcript_insights import analyze_transcript_context
from app.services.transcription import transcribe_audio
from app.services.storage import get_storage
from app.services.usage import record_llm_usage, record_storage_snapshot_usage, record_video_processed_usage
from app.services.youtube import download_youtube_media


PIPELINE_STEPS = (
    "downloading",
    "extracting_audio",
    "transcribing",
    "analyzing",
    "llm_enrichment",
)
ACTIVE_PIPELINE_JOB_STATUSES = {
    "downloading",
    "extracting_audio",
    "transcribing",
    "analyzing",
    "llm_enrichment",
}
MAX_STEP_ATTEMPTS = 3
logger = logging.getLogger(__name__)
LLM_ENRICHMENT_MAX_FAILURES_BEFORE_SKIP = 2
PIPELINE_WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"


class StepExhaustedError(RuntimeError):
    def __init__(self, step_name: str, attempts: int, max_attempts: int):
        self.step_name = step_name
        self.attempts = attempts
        self.max_attempts = max_attempts
        super().__init__(
            f"Etapa '{step_name}' excedeu o limite de tentativas ({attempts}/{max_attempts})"
        )


class PipelineCanceledError(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _serialize_details(details: dict[str, Any] | None) -> str | None:
    if not details:
        return None
    return json.dumps(details, ensure_ascii=False, sort_keys=True)


def _deserialize_details(raw_details: str | None) -> dict[str, Any]:
    if not raw_details:
        return {}
    try:
        loaded = json.loads(raw_details)
    except json.JSONDecodeError:
        return {"raw_details": raw_details}
    return loaded if isinstance(loaded, dict) else {"value": loaded}


def _merge_details(*payloads: dict[str, Any] | None) -> dict[str, Any] | None:
    merged: dict[str, Any] = {}
    for payload in payloads:
        if payload:
            merged.update(payload)
    return merged or None


def _duration_seconds(started_at: datetime | None, completed_at: datetime | None) -> float | None:
    if not started_at or not completed_at:
        return None

    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=UTC)
    return round((completed_at - started_at).total_seconds(), 3)


def _lock_stale_before() -> datetime:
    return _utcnow() - timedelta(seconds=max(1, int(settings.pipeline_lock_stale_seconds or 1)))


def _log_step_event(
    event: str,
    job_id: int,
    step_name: str,
    *,
    attempt: int | None = None,
    status: str | None = None,
    duration_seconds: float | None = None,
    error_message: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "event": event,
        "job_id": job_id,
        "step_name": step_name,
    }
    if attempt is not None:
        payload["attempt"] = attempt
    if status is not None:
        payload["status"] = status
    if duration_seconds is not None:
        payload["duration_seconds"] = duration_seconds
    if error_message:
        payload["error_message"] = error_message
    if details:
        payload["details"] = details

    logger.info(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _log_pipeline_event(event: str, job_id: int, **payload) -> None:
    body: dict[str, Any] = {
        "event": event,
        "job_id": job_id,
    }
    body.update(payload)
    logger.info(json.dumps(body, ensure_ascii=False, sort_keys=True))


def _log_pipeline_event(event: str, job_id: int, **payload) -> None:
    body: dict[str, Any] = {
        "event": event,
        "job_id": job_id,
    }
    body.update(payload)
    logger.info(json.dumps(body, ensure_ascii=False, sort_keys=True))


def _get_or_create_step(db: Session, job_id: int, step_name: str) -> JobStep:
    step = (
        db.query(JobStep)
        .filter(JobStep.job_id == job_id, JobStep.step_name == step_name)
        .first()
    )
    if step:
        return step

    step = JobStep(job_id=job_id, step_name=step_name, status="pending", attempts=0)
    db.add(step)
    db.flush()
    return step


def _try_acquire_job_lock(db: Session, job_id: int, worker_id: str | None = None) -> bool:
    lock_owner = worker_id or PIPELINE_WORKER_ID
    now = _utcnow()
    stale_before = _lock_stale_before()
    updated = (
        db.query(Job)
        .filter(Job.id == job_id)
        .filter(or_(Job.locked_at.is_(None), Job.locked_at < stale_before))
        .update(
            {
                Job.locked_at: now,
                Job.locked_by: lock_owner,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return bool(updated)


def _release_job_lock(db: Session, job: Job | None, worker_id: str | None = None) -> None:
    if not job:
        return
    lock_owner = worker_id or PIPELINE_WORKER_ID
    updated = (
        db.query(Job)
        .filter(Job.id == job.id)
        .filter(or_(Job.locked_by == lock_owner, Job.locked_by.is_(None)))
        .update(
            {
                Job.locked_at: None,
                Job.locked_by: None,
            },
            synchronize_session=False,
        )
    )
    if updated:
        db.commit()


def recover_stale_pipeline_jobs(db: Session) -> int:
    stale_before = _lock_stale_before()
    stale_jobs = (
        db.query(Job)
        .filter(Job.locked_at.is_not(None), Job.locked_at < stale_before)
        .filter(Job.status.in_(ACTIVE_PIPELINE_JOB_STATUSES))
        .all()
    )
    for job in stale_jobs:
        running_steps = (
            db.query(JobStep)
            .filter(JobStep.job_id == job.id, JobStep.status == "running")
            .all()
        )
        for step in running_steps:
            step.status = "failed"
            step.error_message = "Execucao interrompida antes do worker liberar o lock."
            step.completed_at = _utcnow()
            step.details = _serialize_details(
                _merge_details(
                    _deserialize_details(step.details),
                    {
                        "recovered_from_stale_lock": True,
                        "locked_by": job.locked_by,
                    },
                )
            )
        job.status = "pending"
        job.error_message = "Job recuperado apos lock expirado; aguardando retry seguro."
        job.locked_at = None
        job.locked_by = None
    if stale_jobs:
        db.commit()
    return len(stale_jobs)


def _is_cancel_requested(job: Job) -> bool:
    return (job.status or "").strip().lower() == "cancel_requested"


def _ensure_not_canceled(db: Session, job: Job, step_name: str) -> None:
    db.refresh(job)
    if _is_cancel_requested(job):
        raise PipelineCanceledError(f"Cancelamento solicitado durante a etapa '{step_name}'")


def _finalize_canceled_job(
    db: Session,
    job: Job,
    *,
    message: str = "Processamento cancelado pelo usuario.",
) -> None:
    job.status = "canceled"
    job.error_message = message
    db.commit()


def request_job_cancellation(db: Session, job: Job) -> None:
    running_step = (
        db.query(JobStep)
        .filter(JobStep.job_id == job.id, JobStep.status == "running")
        .order_by(JobStep.id.desc())
        .first()
    )
    if running_step:
        job.status = "cancel_requested"
        job.error_message = "Cancelamento solicitado pelo usuario."
        details = _merge_details(
            _deserialize_details(running_step.details),
            {
                "cancel_requested": True,
                "progress_message": "Cancelamento solicitado pelo usuario",
                "heartbeat_at": _utcnow().isoformat(),
            },
        )
        running_step.details = _serialize_details(details)
        db.commit()
        _kick_next_pending_job(job.id)
        return

    _finalize_canceled_job(db, job)
    _kick_next_pending_job(job.id)


def mark_step_running(
    db: Session,
    job: Job,
    step_name: str,
    *,
    force: bool = False,
    details: dict[str, Any] | None = None,
) -> JobStep:
    step = _get_or_create_step(db, job.id, step_name)
    attempts = step.attempts or 0
    if attempts >= MAX_STEP_ATTEMPTS and not force:
        step.status = "exhausted"
        exhaustion_details = _merge_details(
            _deserialize_details(step.details),
            {
                "reason": "retry_limit_reached",
                "max_attempts": MAX_STEP_ATTEMPTS,
                "attempt": attempts,
            },
            details,
        )
        step.details = _serialize_details(exhaustion_details)
        step.completed_at = _utcnow()
        job.status = "failed"
        job.error_message = (
            f"Etapa '{step_name}' excedeu o limite de tentativas ({attempts}/{MAX_STEP_ATTEMPTS})"
        )
        db.commit()
        db.refresh(step)
        _log_step_event(
            "step_exhausted",
            job.id,
            step_name,
            attempt=attempts,
            status=step.status,
            duration_seconds=_duration_seconds(step.started_at, step.completed_at),
            error_message=job.error_message,
            details=exhaustion_details,
        )
        raise StepExhaustedError(step_name, attempts, MAX_STEP_ATTEMPTS)

    step.status = "running"
    step.attempts = attempts + 1
    step.error_message = None
    running_details = _merge_details(
        _deserialize_details(step.details),
        details,
        {
            "attempt": step.attempts,
            "max_attempts": MAX_STEP_ATTEMPTS,
            "forced": force,
        },
    )
    step.details = _serialize_details(running_details)
    step.started_at = _utcnow()
    step.completed_at = None

    job.status = step_name
    db.commit()
    db.refresh(step)
    _log_step_event(
        "step_running",
        job.id,
        step_name,
        attempt=step.attempts,
        status=step.status,
        details=running_details,
    )
    return step


def update_step_progress(
    db: Session,
    job: Job,
    step_name: str,
    *,
    progress_message: str,
    progress_percent: int | float | None = None,
    details: dict[str, Any] | None = None,
) -> JobStep:
    normalized_progress = None
    if progress_percent is not None:
        normalized_progress = max(0, min(100, int(round(float(progress_percent)))))
    step = _get_or_create_step(db, job.id, step_name)
    progress_details = _merge_details(
        _deserialize_details(step.details),
        details,
        {
            "progress_message": progress_message,
            "heartbeat_at": _utcnow().isoformat(),
            "progress_percent": normalized_progress,
        },
    )
    step.details = _serialize_details(progress_details)
    db.commit()
    db.refresh(step)
    _log_step_event(
        "step_progress",
        job.id,
        step_name,
        attempt=step.attempts,
        status=step.status,
        details=progress_details,
    )
    return step


def mark_step_completed(
    db: Session,
    job: Job,
    step_name: str,
    *,
    details: dict[str, Any] | None = None,
) -> JobStep:
    step = _get_or_create_step(db, job.id, step_name)
    step.status = "completed"
    step.error_message = None
    if not step.started_at:
        step.started_at = _utcnow()
    step.completed_at = _utcnow()
    completion_details = _merge_details(
        _deserialize_details(step.details),
        details,
        {
            "attempt": step.attempts or 0,
            "duration_seconds": _duration_seconds(step.started_at, step.completed_at),
        },
    )
    step.details = _serialize_details(completion_details)

    db.commit()
    db.refresh(step)
    _log_step_event(
        "step_completed",
        job.id,
        step_name,
        attempt=step.attempts,
        status=step.status,
        duration_seconds=_duration_seconds(step.started_at, step.completed_at),
        details=completion_details,
    )
    return step


def mark_step_skipped(
    db: Session,
    job: Job,
    step_name: str,
    *,
    details: dict[str, Any] | None = None,
) -> JobStep:
    step = _get_or_create_step(db, job.id, step_name)
    step.status = "skipped"
    step.error_message = None
    if not step.started_at:
        step.started_at = _utcnow()
    step.completed_at = _utcnow()
    skipped_details = _merge_details(
        _deserialize_details(step.details),
        details,
        {
            "attempt": step.attempts or 0,
            "duration_seconds": _duration_seconds(step.started_at, step.completed_at),
        },
    )
    step.details = _serialize_details(skipped_details)

    db.commit()
    db.refresh(step)
    _log_step_event(
        "step_skipped",
        job.id,
        step_name,
        attempt=step.attempts,
        status=step.status,
        duration_seconds=_duration_seconds(step.started_at, step.completed_at),
        details=skipped_details,
    )
    return step


def mark_step_failed(db: Session, job: Job, step_name: str, error: Exception) -> JobStep:
    step = _get_or_create_step(db, job.id, step_name)
    exhausted = (step.attempts or 0) >= MAX_STEP_ATTEMPTS
    step.status = "exhausted" if exhausted else "failed"
    step.error_message = str(error)
    details_payload = _merge_details(
        _deserialize_details(step.details),
        {
            "max_attempts": MAX_STEP_ATTEMPTS,
            "retryable": not exhausted,
        },
    ) or {}
    if not step.started_at:
        step.started_at = _utcnow()
    step.completed_at = _utcnow()
    details_payload["attempt"] = step.attempts or 0
    details_payload["duration_seconds"] = _duration_seconds(step.started_at, step.completed_at)
    step.details = _serialize_details(details_payload)

    job.status = "failed"
    job.error_message = str(error)
    db.commit()
    db.refresh(step)
    _log_step_event(
        "step_failed",
        job.id,
        step_name,
        attempt=step.attempts,
        status=step.status,
        duration_seconds=_duration_seconds(step.started_at, step.completed_at),
        error_message=str(error),
        details=details_payload,
    )
    return step


def get_job_steps(db: Session, job_id: int) -> list[JobStep]:
    return (
        db.query(JobStep)
        .filter(JobStep.job_id == job_id)
        .order_by(JobStep.created_at.asc(), JobStep.id.asc())
        .all()
    )


def get_exhausted_steps(db: Session, job_id: int) -> list[JobStep]:
    return (
        db.query(JobStep)
        .filter(JobStep.job_id == job_id, JobStep.status == "exhausted")
        .order_by(JobStep.created_at.asc(), JobStep.id.asc())
        .all()
    )


def validate_step_name(step_name: str) -> str:
    normalized = (step_name or "").strip().lower()
    if normalized not in PIPELINE_STEPS:
        raise ValueError(f"Etapa inválida: {step_name}")
    return normalized


def get_steps_from(step_name: str) -> tuple[str, ...]:
    normalized = validate_step_name(step_name)
    start_index = PIPELINE_STEPS.index(normalized)
    return PIPELINE_STEPS[start_index:]


def reset_pipeline_state_from_step(
    db: Session,
    job: Job,
    step_name: str,
    *,
    reset_attempts: bool = False,
) -> list[JobStep]:
    steps_to_reset = get_steps_from(step_name)
    rows = (
        db.query(JobStep)
        .filter(JobStep.job_id == job.id, JobStep.step_name.in_(steps_to_reset))
        .all()
    )

    for step in rows:
        step.status = "pending"
        step.error_message = None
        step.details = None
        step.started_at = None
        step.completed_at = None
        if reset_attempts:
            step.attempts = 0

    if "downloading" in steps_to_reset:
        job.video_path = None
        job.title = None
    if "extracting_audio" in steps_to_reset:
        job.audio_path = None
    if "transcribing" in steps_to_reset:
        job.transcript_path = None
    if "analyzing" in steps_to_reset:
        job.detected_niche = None
        job.niche_confidence = None
        db.query(Candidate).filter(Candidate.job_id == job.id).delete()
    if "llm_enrichment" in steps_to_reset:
        job.transcript_insights = None

    job.status = "pending"
    job.error_message = None
    db.commit()
    return rows


def _path_exists(path_value: str | None) -> bool:
    return get_storage().exists(path_value)


def _transcript_duration_seconds(transcript_data: dict[str, Any]) -> float | None:
    segments = transcript_data.get("segments") or []
    ends = []
    for segment in segments:
        try:
            ends.append(float(segment.get("end", 0)))
        except (TypeError, ValueError):
            continue
    if ends:
        return max(ends)
    text = (transcript_data.get("text") or "").strip()
    return 0.0 if text else None


def _build_queue_message(queue_position: int) -> str:
    if queue_position <= 1:
        return "Aguardando vaga na fila de processamento."
    return f"Aguardando vaga na fila de processamento ({queue_position - 1} na frente)."


def _count_active_pipeline_jobs(db: Session, *, exclude_job_id: int | None = None) -> int:
    query = db.query(Job).filter(Job.status.in_(ACTIVE_PIPELINE_JOB_STATUSES))
    if exclude_job_id is not None:
        query = query.filter(Job.id != exclude_job_id)
    return query.count()


def _compute_pending_queue_position(db: Session, job: Job) -> int:
    pending_jobs = (
        db.query(Job)
        .filter(Job.status == "pending")
        .order_by(Job.created_at.asc(), Job.id.asc())
        .all()
    )
    for index, pending_job in enumerate(pending_jobs, start=1):
        if pending_job.id == job.id:
            return index
    return len(pending_jobs) + 1


def _try_acquire_pipeline_slot(db: Session, job: Job) -> bool:
    max_jobs = max(1, int(settings.max_concurrent_pipeline_jobs or 1))
    active_jobs = _count_active_pipeline_jobs(db, exclude_job_id=job.id)
    if active_jobs < max_jobs:
        if job.status == "pending" and (job.error_message or "").startswith("Aguardando vaga na fila"):
            job.error_message = None
            db.commit()
        return True

    queue_position = _compute_pending_queue_position(db, job)
    job.status = "pending"
    job.error_message = _build_queue_message(queue_position)
    db.commit()
    _log_step_event(
        "job_queued",
        job.id,
        "pipeline",
        status=job.status,
        details={
            "queue_position": queue_position,
            "active_jobs": active_jobs,
            "max_concurrent_pipeline_jobs": max_jobs,
        },
    )
    return False


def _kick_next_pending_job(current_job_id: int | None = None) -> None:
    db = SessionLocal()
    try:
        available_slots = max(
            0,
            int(settings.max_concurrent_pipeline_jobs or 1) - _count_active_pipeline_jobs(db),
        )
        if available_slots <= 0:
            return

        queued_jobs = (
            db.query(Job)
            .filter(Job.status == "pending")
            .order_by(Job.created_at.asc(), Job.id.asc())
            .all()
        )
    finally:
        db.close()

    launched = 0
    for queued_job in queued_jobs:
        if current_job_id is not None and queued_job.id == current_job_id:
            continue
        if launched >= available_slots:
            break
        process_job_pipeline(queued_job.id)
        launched += 1


def _run_download_step(db: Session, job: Job, *, force: bool = False) -> None:
    if _path_exists(job.video_path):
        mark_step_skipped(
            db,
            job,
            "downloading",
            details={"reason": "video_path já disponível", "video_path": job.video_path},
        )
        return

    mark_step_running(
        db,
        job,
        "downloading",
        force=force,
        details={"source_value": job.source_value},
    )
    media = download_youtube_media(job.source_value, job.id)
    job.video_path = media["video_path"]
    job.title = media["title"]
    db.commit()
    mark_step_completed(
        db,
        job,
        "downloading",
        details={"video_path": job.video_path, "title": job.title},
    )


def _run_extract_audio_step(db: Session, job: Job, *, force: bool = False) -> None:
    if _path_exists(job.audio_path):
        mark_step_skipped(
            db,
            job,
            "extracting_audio",
            details={"reason": "audio_path já disponível", "audio_path": job.audio_path},
        )
        return

    mark_step_running(
        db,
        job,
        "extracting_audio",
        force=force,
        details={"video_path": job.video_path},
    )
    job.audio_path = extract_audio_from_video(job.video_path, job.id)
    db.commit()
    mark_step_completed(
        db,
        job,
        "extracting_audio",
        details={"audio_path": job.audio_path},
    )


def _run_transcription_step(db: Session, job: Job, *, force: bool = False) -> None:
    if _path_exists(job.transcript_path):
        mark_step_skipped(
            db,
            job,
            "transcribing",
            details={"reason": "transcript_path já disponível", "transcript_path": job.transcript_path},
        )
        return

    mark_step_running(
        db,
        job,
        "transcribing",
        force=force,
        details={"audio_path": job.audio_path},
    )
    update_step_progress(
        db,
        job,
        "transcribing",
        progress_message="Preparando transcricao do audio",
        details={"audio_path": job.audio_path},
    )

    def _transcription_progress(message: str) -> None:
        _ensure_not_canceled(db, job, "transcribing")
        update_step_progress(
            db,
            job,
            "transcribing",
            progress_message=message,
        )

    job.transcript_path = transcribe_audio(
        job.audio_path,
        job.id,
        progress_callback=_transcription_progress,
    )
    transcript_data = load_transcript(job.transcript_path)
    record_video_processed_usage(
        db,
        job,
        duration_seconds=_transcript_duration_seconds(transcript_data),
    )
    db.commit()
    mark_step_completed(
        db,
        job,
        "transcribing",
        details={"transcript_path": job.transcript_path},
    )


def _run_analyze_step(
    db: Session,
    job: Job,
    *,
    force: bool = False,
) -> None:
    if job.detected_niche and job.niche_confidence:
        mark_step_skipped(
            db,
            job,
            "analyzing",
            details={
                "reason": "nicho já detectado",
                "detected_niche": job.detected_niche,
                "niche_confidence": job.niche_confidence,
                "has_transcript_insights": bool(job.transcript_insights),
            },
        )
        return

    mark_step_running(
        db,
        job,
        "analyzing",
        force=force,
        details={"transcript_path": job.transcript_path},
    )
    update_step_progress(
        db,
        job,
        "analyzing",
        progress_message="Carregando transcricao para analise",
        progress_percent=18,
    )
    transcript_data = load_transcript(job.transcript_path)
    transcript_text = transcript_data.get("text", "")

    update_step_progress(
        db,
        job,
        "analyzing",
        progress_message="Detectando nicho editorial",
        progress_percent=34,
        details={"transcript_characters": len(transcript_text)},
    )
    niche_result = detect_niche(job.title, transcript_text, db=db)
    job.detected_niche = niche_result["niche"]
    job.niche_confidence = niche_result["confidence"]

    candidate_summary = {}
    if _path_exists(job.transcript_path):
        update_step_progress(
            db,
            job,
            "analyzing",
            progress_message="Gerando candidatos iniciais",
            progress_percent=52,
        )
        def _candidate_progress(message: str, percent: int | float | None = None) -> None:
            update_step_progress(
                db,
                job,
                "analyzing",
                progress_message=message,
                progress_percent=percent,
            )

        candidate_summary = ensure_default_candidates_for_job(
            db,
            job,
            modes=("short",),
            force=force,
            progress_callback=_candidate_progress,
        )

    mark_step_completed(
        db,
        job,
        "analyzing",
        details={
            "detected_niche": job.detected_niche,
            "niche_confidence": job.niche_confidence,
            "generated_candidates": candidate_summary,
        },
    )


def complete_analysis_without_llm(db: Session, job: Job, *, force: bool = True) -> None:
    if not job.transcript_path:
        raise RuntimeError("Job precisa de transcricao para concluir a analise sem LLM")

    mark_step_running(
        db,
        job,
        "llm_enrichment",
        force=force,
        details={"transcript_path": job.transcript_path, "skip_llm_insights": True},
    )
    update_step_progress(
        db,
        job,
        "llm_enrichment",
        progress_message="Pulando enriquecimento da LLM por solicitacao do usuario",
        details={"skip_llm_insights": True},
    )
    job.transcript_insights = None
    db.commit()
    mark_step_completed(
        db,
        job,
        "llm_enrichment",
        details={
            "insights_generated": False,
            "llm_insights_skipped": True,
            "llm_insights_error": "skipped_by_user",
            "skip_llm_insights": True,
        },
    )


def _complete_llm_enrichment_as_skipped(
    db: Session,
    job: Job,
    *,
    reason: str,
    force: bool = False,
    details: dict[str, Any] | None = None,
) -> None:
    mark_step_running(
        db,
        job,
        "llm_enrichment",
        force=force,
        details=_merge_details({"transcript_path": job.transcript_path}, details),
    )
    update_step_progress(
        db,
        job,
        "llm_enrichment",
        progress_message=reason,
        details=_merge_details(details, {"skip_llm_insights": True}),
    )
    job.transcript_insights = None
    db.commit()
    mark_step_completed(
        db,
        job,
        "llm_enrichment",
        details=_merge_details(
            details,
            {
                "insights_generated": False,
                "llm_insights_skipped": True,
                "llm_insights_error": reason,
                "skip_llm_insights": True,
            },
        ),
    )


def _run_llm_enrichment_step(
    db: Session,
    job: Job,
    *,
    force: bool = False,
) -> None:
    if job.transcript_insights:
        mark_step_skipped(
            db,
            job,
            "llm_enrichment",
            details={
                "reason": "transcript_insights ja disponiveis",
                "insights_generated": True,
            },
        )
        return

    existing_step = _get_or_create_step(db, job.id, "llm_enrichment")
    if (existing_step.attempts or 0) >= LLM_ENRICHMENT_MAX_FAILURES_BEFORE_SKIP and not force:
        _complete_llm_enrichment_as_skipped(
            db,
            job,
            reason="LLM pulada apos repetidas falhas recentes",
            details={
                "llm_circuit_breaker_opened": True,
                "previous_attempts": existing_step.attempts or 0,
            },
        )
        return

    mark_step_running(
        db,
        job,
        "llm_enrichment",
        force=force,
        details={"transcript_path": job.transcript_path},
    )
    update_step_progress(
        db,
        job,
        "llm_enrichment",
        progress_message="Gerando insights da transcricao",
        details={
            "detected_niche": job.detected_niche,
            "niche_confidence": job.niche_confidence,
        },
    )

    transcript_data = load_transcript(job.transcript_path)
    transcript_text = transcript_data.get("text", "")
    llm_insights_error = None
    try:
        _ensure_not_canceled(db, job, "llm_enrichment")
        insights = analyze_transcript_context(job.title, transcript_text)
        record_llm_usage(
            db,
            job,
            provider=settings.llm_provider,
            model=settings.llm_model,
        )
    except Exception as exc:
        llm_insights_error = str(exc)
        insights = {}

    job.transcript_insights = json.dumps(insights, ensure_ascii=False) if insights else None
    db.commit()
    mark_step_completed(
        db,
        job,
        "llm_enrichment",
        details={
            "insights_generated": bool(job.transcript_insights),
            "llm_insights_skipped": not bool(job.transcript_insights),
            "llm_insights_error": llm_insights_error,
        },
    )


def process_job_pipeline(
    job_id: int,
    force: bool = False,
    start_from_step: str | None = None,
    worker_id: str | None = None,
):
    db = SessionLocal()
    current_step = None
    job = None
    lock_acquired = False

    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            _log_pipeline_event("pipeline_job_not_found", job_id)
            return

        if not _try_acquire_job_lock(db, job.id, worker_id):
            _log_pipeline_event("pipeline_job_locked_elsewhere", job.id, worker_id=worker_id or PIPELINE_WORKER_ID)
            return
        lock_acquired = True
        db.refresh(job)

        _log_pipeline_event("pipeline_job_started", job.id, worker_id=worker_id or PIPELINE_WORKER_ID)
        job.error_message = None
        if job.status == "failed":
            job.status = "pending"
            db.commit()

        exhausted_steps = get_exhausted_steps(db, job.id)
        if exhausted_steps and not force:
            allowed_exhausted = set(get_steps_from(start_from_step)) if start_from_step else set()
            blocking_exhausted = [
                step for step in exhausted_steps
                if not allowed_exhausted or step.step_name not in allowed_exhausted
            ]
            if blocking_exhausted:
                names = ", ".join(step.step_name for step in blocking_exhausted)
                raise StepExhaustedError(
                    names,
                    blocking_exhausted[0].attempts or MAX_STEP_ATTEMPTS,
                    MAX_STEP_ATTEMPTS,
                )

        if not _try_acquire_pipeline_slot(db, job):
            _log_pipeline_event("pipeline_job_waiting_for_slot", job.id)
            return

        steps_to_run = get_steps_from(start_from_step) if start_from_step else PIPELINE_STEPS

        for step_name in steps_to_run:
            _ensure_not_canceled(db, job, step_name)
            current_step = step_name
            if step_name == "downloading":
                _run_download_step(db, job, force=force)
                _log_pipeline_event("pipeline_step_finished", job.id, step_name=step_name, video_path=job.video_path)
            elif step_name == "extracting_audio":
                _run_extract_audio_step(db, job, force=force)
                _log_pipeline_event("pipeline_step_finished", job.id, step_name=step_name, audio_path=job.audio_path)
            elif step_name == "transcribing":
                _run_transcription_step(db, job, force=force)
                _log_pipeline_event("pipeline_step_finished", job.id, step_name=step_name, transcript_path=job.transcript_path)
            elif step_name == "analyzing":
                _run_analyze_step(db, job, force=force)
                _log_pipeline_event(
                    "pipeline_step_finished",
                    job.id,
                    step_name=step_name,
                    detected_niche=job.detected_niche,
                    niche_confidence=job.niche_confidence,
                )
            elif step_name == "llm_enrichment":
                _run_llm_enrichment_step(db, job, force=force)
                _log_pipeline_event(
                    "pipeline_step_finished",
                    job.id,
                    step_name=step_name,
                    insights_generated=bool(job.transcript_insights),
                )

        job.status = "done"
        job.error_message = None
        db.commit()
        if job.workspace_id is not None:
            record_storage_snapshot_usage(db, job.workspace_id)
        _log_pipeline_event("pipeline_job_completed", job.id)

    except Exception as e:
        _log_pipeline_event("pipeline_job_failed", job_id, step_name=current_step, error=str(e))
        job = db.query(Job).filter(Job.id == job_id).first()
        if job:
            if isinstance(e, PipelineCanceledError):
                if current_step:
                    mark_step_failed(db, job, current_step, RuntimeError("Cancelado pelo usuario"))
                job.status = "canceled"
                job.error_message = "Processamento cancelado pelo usuario."
                db.commit()
            elif isinstance(e, StepExhaustedError):
                job.status = "failed"
                job.error_message = str(e)
                db.commit()
            elif current_step:
                mark_step_failed(db, job, current_step, e)
            else:
                job.status = "failed"
                job.error_message = str(e)
                db.commit()
    finally:
        if lock_acquired:
            _release_job_lock(db, job, worker_id)
        db.close()
        _kick_next_pending_job(job_id)
