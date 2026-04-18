import json
import logging
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.models.job import Job
from app.models.job_step import JobStep
from app.services.audio import extract_audio_from_video
from app.services.candidates import ensure_default_candidates_for_job
from app.services.niche_classifier import detect_niche
from app.services.segmentation import load_transcript
from app.services.transcript_insights import analyze_transcript_context
from app.services.transcription import transcribe_audio
from app.services.youtube import download_youtube_media


PIPELINE_STEPS = (
    "downloading",
    "extracting_audio",
    "transcribing",
    "analyzing",
)
MAX_STEP_ATTEMPTS = 3
logger = logging.getLogger(__name__)


class StepExhaustedError(RuntimeError):
    def __init__(self, step_name: str, attempts: int, max_attempts: int):
        self.step_name = step_name
        self.attempts = attempts
        self.max_attempts = max_attempts
        super().__init__(
            f"Etapa '{step_name}' excedeu o limite de tentativas ({attempts}/{max_attempts})"
        )


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

    job.status = "pending"
    job.error_message = None
    db.commit()
    return rows


def _path_exists(path_value: str | None) -> bool:
    return bool(path_value) and Path(path_value).exists()


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
    job.transcript_path = transcribe_audio(job.audio_path, job.id)
    db.commit()
    mark_step_completed(
        db,
        job,
        "transcribing",
        details={"transcript_path": job.transcript_path},
    )


def _run_analyze_step(db: Session, job: Job, *, force: bool = False) -> None:
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
    transcript_data = load_transcript(job.transcript_path)
    transcript_text = transcript_data.get("text", "")

    niche_result = detect_niche(job.title, transcript_text, db=db)
    job.detected_niche = niche_result["niche"]
    job.niche_confidence = niche_result["confidence"]
    try:
        insights = analyze_transcript_context(job.title, transcript_text)
    except Exception:
        insights = {}
    job.transcript_insights = json.dumps(insights, ensure_ascii=False) if insights else None
    db.commit()

    candidate_summary = {}
    if _path_exists(job.transcript_path):
        candidate_summary = ensure_default_candidates_for_job(db, job, modes=("short",), force=force)

    mark_step_completed(
        db,
        job,
        "analyzing",
        details={
            "detected_niche": job.detected_niche,
            "niche_confidence": job.niche_confidence,
            "insights_generated": bool(job.transcript_insights),
            "generated_candidates": candidate_summary,
        },
    )


def process_job_pipeline(
    job_id: int,
    force: bool = False,
    start_from_step: str | None = None,
):
    db = SessionLocal()
    current_step = None

    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            print(f"[PIPELINE] Job {job_id} não encontrado", flush=True)
            return

        print(f"[PIPELINE] Iniciando job {job.id}", flush=True)
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

        steps_to_run = get_steps_from(start_from_step) if start_from_step else PIPELINE_STEPS

        for step_name in steps_to_run:
            current_step = step_name
            if step_name == "downloading":
                _run_download_step(db, job, force=force)
                print(f"[PIPELINE] status=downloading | video_path={job.video_path}", flush=True)
            elif step_name == "extracting_audio":
                _run_extract_audio_step(db, job, force=force)
                print(f"[PIPELINE] status=extracting_audio | audio_path={job.audio_path}", flush=True)
            elif step_name == "transcribing":
                _run_transcription_step(db, job, force=force)
                print(f"[PIPELINE] status=transcribing | transcript_path={job.transcript_path}", flush=True)
            elif step_name == "analyzing":
                _run_analyze_step(db, job, force=force)
                print(
                    f"[PIPELINE] status=analyzing | detected_niche={job.detected_niche} | "
                    f"niche_confidence={job.niche_confidence}",
                    flush=True,
                )

        job.status = "done"
        job.error_message = None
        db.commit()
        print(f"[PIPELINE] status=done | job_id={job.id}", flush=True)

    except Exception as e:
        print(f"[PIPELINE] ERRO em {current_step}: {e}", flush=True)
        job = db.query(Job).filter(Job.id == job_id).first()
        if job:
            if isinstance(e, StepExhaustedError):
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
        db.close()
