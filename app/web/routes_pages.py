import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.deps import get_current_workspace, require_current_user, require_current_workspace
from app.db.database import get_db
from app.models.candidate import Candidate
from app.models.clip import Clip
from app.models.job import Job
from app.models.job_step import JobStep
from app.models.user import User
from app.models.workspace import Workspace
from app.models.workspace_member import WorkspaceMember
from app.core.config import settings
from app.services.access import TRIAL_MAX_VIDEO_MINUTES, ensure_workspace_can_create_job
from app.services.candidates import get_candidates_for_job, regenerate_candidates_for_job
from app.services.exports import list_job_export_bundles
from app.services.niche_learning import (
    get_feedback_profile_for_niche,
    get_learned_keywords_for_niche,
    learn_keywords_for_niche,
)
from app.services.analysis_calibration import build_analysis_calibration_profile
from app.services.niche_registry import (
    approve_niche,
    archive_niche,
    create_pending_niche,
    get_niche_profile,
    list_niche_definitions,
    reject_niche,
)
from app.services.pipeline import (
    MAX_STEP_ATTEMPTS,
    complete_analysis_without_llm,
    get_job_steps,
    request_job_cancellation,
)
from app.services.publication import PUBLICATION_STATUS_LABELS, normalize_publication_status
from app.services.queue import enqueue_pipeline_job
from app.services.quota import ensure_workspace_can_start_job, get_workspace_quota_status
from app.services.render_presets import DEFAULT_PRESET, list_render_presets
from app.services.render_workflow import render_candidate_clip, render_manual_clip
from app.services.serializers import serialize_candidate, serialize_clip
from app.services.system_diagnostics import build_runtime_readiness, build_system_diagnostics
from app.services.scoring import score_candidates
from app.services.segmentation import build_candidate_windows, load_segments
from app.services.media import probe_video_duration_seconds
from app.services.storage import get_storage, normalize_storage_key
from app.services.youtube import fetch_youtube_metadata
from app.web.security import validate_csrf_request
from app.web.template_utils import build_templates
from app.utils.media_urls import build_static_url
from app.utils.timecodes import parse_timecode_to_seconds


router = APIRouter(tags=["pages"], dependencies=[Depends(validate_csrf_request)])
templates = build_templates()
STEP_STALE_HEARTBEAT_SECONDS = 900
JOB_AUTO_REFRESH_STATUSES = {
    "pending",
    "downloading",
    "extracting_audio",
    "transcribing",
    "analyzing",
    "llm_enrichment",
    "rendering",
    "cancel_requested",
}
JOB_AUTO_REFRESH_INTERVAL_MS = 15000


def has_active_jobs(jobs: list[Job]) -> bool:
    active_statuses = {
        "pending",
        "downloading",
        "extracting_audio",
        "transcribing",
        "analyzing",
        "llm_enrichment",
        "rendering",
        "cancel_requested",
    }
    return any(job.status in active_statuses for job in jobs)


PIPELINE_STEP_SEQUENCE = (
    "downloading",
    "extracting_audio",
    "transcribing",
    "analyzing",
)


def _normalize_mode(mode: str) -> str:
    normalized = mode.lower().strip()
    return normalized if normalized in {"short", "long"} else "short"


def _parse_step_details(raw_details: str | None) -> dict:
    if not raw_details:
        return {}
    try:
        payload = json.loads(raw_details)
    except json.JSONDecodeError:
        return {"raw_details": raw_details}
    return payload if isinstance(payload, dict) else {"value": payload}


def _heartbeat_age_seconds(raw_value: str | None) -> float | None:
    if not raw_value:
        return None
    try:
        heartbeat_dt = datetime.fromisoformat(str(raw_value))
    except ValueError:
        return None
    if heartbeat_dt.tzinfo is not None:
        heartbeat_dt = heartbeat_dt.astimezone(UTC).replace(tzinfo=None)
    return (datetime.utcnow() - heartbeat_dt).total_seconds()


def _build_timecode_from_parts(hours: str | None, minutes: str | None, seconds: str | None) -> str:
    hour_value = (hours or "").strip() or "0"
    minute_value = (minutes or "").strip() or "0"
    second_value = (seconds or "").strip() or "0"
    return f"{hour_value}:{minute_value}:{second_value}"


def _get_candidate_or_404(db: Session, candidate_id: int) -> Candidate:
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidato não encontrado")
    return candidate


def _get_job_for_workspace_or_404(db: Session, job_id: int, workspace: Workspace) -> Job:
    job = db.query(Job).filter(Job.id == job_id, Job.workspace_id == workspace.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job nÃ£o encontrado")
    return job


def _job_view_url(
    job_id: int,
    *,
    mode: str | None = None,
    render_preset: str | None = None,
    message: str | None = None,
    level: str = "success",
) -> str:
    params: dict[str, str] = {}
    if mode:
        params["mode"] = _normalize_mode(mode)
    if render_preset:
        params["render_preset"] = render_preset
    if message:
        params["message"] = message
        params["message_level"] = level
    query = urlencode(params)
    return f"/jobs/{job_id}/view?{query}" if query else f"/jobs/{job_id}/view"


def _dashboard_url(message: str | None = None, level: str = "success") -> str:
    params: dict[str, str] = {}
    if message:
        params["message"] = message
        params["message_level"] = level
    query = urlencode(params)
    return f"/dashboard?{query}" if query else "/dashboard"


def _billing_activation_url() -> str:
    params = urlencode(
        {
            "message": (
                f"Seu workspace pode testar 1 video de ate {TRIAL_MAX_VIDEO_MINUTES} minutos sem cartao. "
                "Depois disso, cadastre um cartao para continuar."
            ),
            "level": "warning",
        }
    )
    return f"/billing?{params}"


def _is_billing_activation_message(message: str) -> bool:
    normalized = (message or "").lower()
    return any(
        token in normalized
        for token in ("cartao", "teste gratis", "30 minutos", "videos maiores")
    )


def _get_ranked_candidates(db: Session, job: Job, mode: str) -> list[dict]:
    raw_segments = load_segments(job.transcript_path)
    candidates = build_candidate_windows(raw_segments, mode=mode)
    niche = job.detected_niche or "geral"
    niche_profile = get_niche_profile(db, niche, workspace_id=job.workspace_id)
    learned_keywords = get_learned_keywords_for_niche(db, niche, workspace_id=job.workspace_id)
    feedback_profile = get_feedback_profile_for_niche(db, niche, mode, workspace_id=job.workspace_id)
    transcript_insights = json.loads(job.transcript_insights) if job.transcript_insights else None
    calibration_profile = build_analysis_calibration_profile(db, niche=niche, mode=mode)
    return score_candidates(
        candidates,
        mode=mode,
        niche=niche,
        niche_profile=niche_profile,
        learned_keywords=learned_keywords,
        feedback_profile=feedback_profile,
        transcript_insights=transcript_insights,
        calibration_profile=calibration_profile,
    )


def _ensure_page_candidates(
    db: Session,
    job: Job,
    mode: str,
) -> list[Candidate]:
    saved_candidates = get_candidates_for_job(db, job.id, mode)
    if saved_candidates:
        return saved_candidates
    return regenerate_candidates_for_job(db, job, mode=mode)


def _get_candidates_for_job_view(
    db: Session,
    job: Job,
    mode: str,
) -> list[Candidate]:
    saved_candidates = get_candidates_for_job(db, job.id, mode)
    if saved_candidates:
        return saved_candidates
    if job.status == "done" and job.transcript_path and Path(job.transcript_path).exists():
        return regenerate_candidates_for_job(db, job, mode=mode)
    return []


def format_seconds_to_mmss(seconds: float | int | None) -> str:
    if seconds is None:
        return "--:--"

    total = int(round(float(seconds)))
    minutes = total // 60
    secs = total % 60
    return f"{minutes:02}:{secs:02}"


def filter_jobs_for_view(jobs: list[Job], view_filter: str) -> list[Job]:
    normalized = (view_filter or "all").strip().lower()
    if normalized == "active":
        return [job for job in jobs if job.status not in {"done", "failed"}]
    if normalized == "done":
        return [job for job in jobs if job.status == "done"]
    if normalized == "failed":
        return [job for job in jobs if job.status == "failed"]
    return jobs


def search_jobs_for_view(jobs: list[Job], search_query: str) -> list[Job]:
    normalized = (search_query or "").strip().lower()
    if not normalized:
        return jobs

    def _matches(job: Job) -> bool:
        title = (job.title or "").lower()
        source = (job.source_value or "").lower()
        return normalized in title or normalized in source or normalized in f"job {job.id}"

    return [job for job in jobs if _matches(job)]


def enrich_jobs_with_progress(db: Session, jobs: list[Job]) -> list[Job]:
    if not jobs:
        return jobs

    step_rows = (
        db.query(JobStep)
        .filter(JobStep.job_id.in_([job.id for job in jobs]))
        .order_by(JobStep.created_at.asc(), JobStep.id.asc())
        .all()
    )

    grouped_steps: dict[int, dict[str, JobStep]] = {}
    for step in step_rows:
        grouped_steps.setdefault(step.job_id, {})[step.step_name] = step

    total_steps = len(PIPELINE_STEP_SEQUENCE) or 1

    for job in jobs:
        progress_value = 5
        step_map = grouped_steps.get(job.id, {})

        completed_units = 0.0
        active_step_name = None
        for index, step_name in enumerate(PIPELINE_STEP_SEQUENCE):
            step = step_map.get(step_name)
            if not step:
                continue
            if step.status in {"completed", "skipped"}:
                completed_units += 1.0
                continue
            if step.status == "running":
                completed_units += 0.55
                active_step_name = step_name
                break
            if step.status in {"failed", "exhausted"}:
                active_step_name = step_name
                break
            if step.status == "pending":
                active_step_name = step_name
                break

        if job.status == "done":
            progress_value = 100
        elif job.status == "failed":
            progress_value = max(5, min(95, round((completed_units / total_steps) * 100)))
        else:
            progress_value = max(5, min(95, round((completed_units / total_steps) * 100)))

        setattr(job, "progress_value", progress_value)
        setattr(job, "active_step_name", active_step_name or job.status)

    return jobs


def _build_niche_flash(message: str | None, level: str | None) -> dict | None:
    if not message:
        return None
    return {
        "message": message,
        "level": level or "info",
    }


def _niche_redirect(message: str, level: str = "info") -> RedirectResponse:
    params = urlencode({"message": message, "level": level})
    return RedirectResponse(url=f"/nichos?{params}", status_code=303)


def build_dashboard_summary(db: Session, jobs: list[Job]) -> dict:
    if not jobs:
        return {
            "total_jobs": 0,
            "active_jobs": 0,
            "queued_jobs": 0,
            "failed_jobs": 0,
            "canceled_jobs": 0,
            "jobs_with_approved": 0,
            "jobs_with_clips": 0,
            "jobs_with_exports": 0,
            "jobs_ready_to_publish": 0,
            "jobs_published": 0,
        }

    job_ids = [job.id for job in jobs]
    candidates = db.query(Candidate).filter(Candidate.job_id.in_(job_ids)).all()
    clips = db.query(Clip).filter(Clip.job_id.in_(job_ids)).all()

    jobs_with_approved = {candidate.job_id for candidate in candidates if candidate.status == "approved"}
    jobs_with_clips = {clip.job_id for clip in clips}
    jobs_ready_to_publish = {clip.job_id for clip in clips if clip.publication_status == "ready"}
    jobs_published = {clip.job_id for clip in clips if clip.publication_status == "published"}
    jobs_with_exports = {
        job.id for job in jobs
        if list_job_export_bundles(job.id)
    }

    return {
        "total_jobs": len(jobs),
        "active_jobs": sum(1 for job in jobs if job.status not in {"done", "failed"}),
        "queued_jobs": sum(
            1
            for job in jobs
            if job.status == "pending" and (job.error_message or "").startswith("Aguardando vaga na fila")
        ),
        "failed_jobs": sum(1 for job in jobs if job.status == "failed"),
        "canceled_jobs": sum(1 for job in jobs if job.status == "canceled"),
        "jobs_with_approved": len(jobs_with_approved),
        "jobs_with_clips": len(jobs_with_clips),
        "jobs_with_exports": len(jobs_with_exports),
        "jobs_ready_to_publish": len(jobs_ready_to_publish),
        "jobs_published": len(jobs_published),
    }


def build_pipeline_health_summary(db: Session, jobs: list[Job]) -> dict:
    job_ids = [job.id for job in jobs]
    step_rows = []
    if job_ids:
        step_rows = db.query(JobStep).filter(JobStep.job_id.in_(job_ids)).all()

    durations_by_step: dict[str, list[float]] = {}
    stale_running_steps = 0
    for step in step_rows:
        details = _parse_step_details(step.details)
        duration = details.get("duration_seconds")
        if isinstance(duration, (int, float)):
            durations_by_step.setdefault(step.step_name, []).append(float(duration))
        age_seconds = _heartbeat_age_seconds(details.get("heartbeat_at"))
        if step.status == "running" and age_seconds is not None and age_seconds >= STEP_STALE_HEARTBEAT_SECONDS:
            stale_running_steps += 1

    average_durations = {
        step_name: round(sum(values) / len(values), 1)
        for step_name, values in durations_by_step.items()
        if values
    }

    longest_step = None
    if average_durations:
        longest_step = max(average_durations.items(), key=lambda item: item[1])

    queued_jobs = [
        job for job in jobs
        if job.status == "pending" and (job.error_message or "").startswith("Aguardando vaga na fila")
    ]
    return {
        "queued_jobs": len(queued_jobs),
        "active_jobs": sum(
            1
            for job in jobs
            if job.status in {"downloading", "extracting_audio", "transcribing", "analyzing", "llm_enrichment", "cancel_requested"}
        ),
        "canceled_jobs": sum(1 for job in jobs if job.status == "canceled"),
        "failed_jobs": sum(1 for job in jobs if job.status == "failed"),
        "avg_transcribing_seconds": average_durations.get("transcribing"),
        "avg_analyzing_seconds": average_durations.get("analyzing"),
        "avg_llm_seconds": average_durations.get("llm_enrichment"),
        "slowest_step_name": longest_step[0] if longest_step else None,
        "slowest_step_seconds": longest_step[1] if longest_step else None,
        "stale_running_steps": stale_running_steps,
    }


def build_job_priority_groups(db: Session, jobs: list[Job]) -> dict[str, list[Job]]:
    if not jobs:
        return {
            "stale_jobs": [],
            "failed_jobs": [],
            "queued_jobs": [],
            "active_jobs": [],
            "completed_jobs": [],
            "canceled_jobs": [],
        }

    job_ids = [job.id for job in jobs]
    step_rows = (
        db.query(JobStep)
        .filter(JobStep.job_id.in_(job_ids))
        .order_by(JobStep.created_at.asc(), JobStep.id.asc())
        .all()
    )

    stale_job_ids: set[int] = set()
    for step in step_rows:
        details = _parse_step_details(step.details)
        heartbeat_at = details.get("heartbeat_at")
        if step.status != "running" or not heartbeat_at:
            continue
        age_seconds = _heartbeat_age_seconds(heartbeat_at)
        if age_seconds is not None and age_seconds >= STEP_STALE_HEARTBEAT_SECONDS:
            stale_job_ids.add(step.job_id)

    stale_jobs = [job for job in jobs if job.id in stale_job_ids]
    failed_jobs = [job for job in jobs if job.status == "failed" and job.id not in stale_job_ids]
    queued_jobs = [
        job for job in jobs
        if job.status == "pending"
        and (job.error_message or "").startswith("Aguardando vaga na fila")
        and job.id not in stale_job_ids
    ]
    canceled_jobs = [job for job in jobs if job.status == "canceled"]
    active_jobs = [
        job for job in jobs
        if job.status in {"pending", "downloading", "extracting_audio", "transcribing", "analyzing", "llm_enrichment", "cancel_requested", "rendering"}
        and job.id not in stale_job_ids
        and job.status != "failed"
        and not ((job.error_message or "").startswith("Aguardando vaga na fila") and job.status == "pending")
    ]
    completed_jobs = [job for job in jobs if job.status == "done"]

    return {
        "stale_jobs": stale_jobs,
        "failed_jobs": failed_jobs,
        "queued_jobs": queued_jobs,
        "active_jobs": active_jobs,
        "completed_jobs": completed_jobs,
        "canceled_jobs": canceled_jobs,
    }


def build_publication_board(db: Session, jobs: list[Job]) -> dict:
    if not jobs:
        return {
            "ready_jobs": [],
            "published_jobs": [],
            "discarded_jobs": [],
        }

    job_map = {job.id: job for job in jobs}
    clips = (
        db.query(Clip)
        .filter(Clip.job_id.in_(job_map.keys()))
        .order_by(Clip.created_at.desc())
        .all()
    )

    grouped: dict[int, list[Clip]] = {}
    for clip in clips:
        grouped.setdefault(clip.job_id, []).append(clip)

    def _build_rows(target_status: str) -> list[dict]:
        rows = []
        for job_id, job_clips in grouped.items():
            matching = [clip for clip in job_clips if clip.publication_status == target_status]
            if not matching:
                continue
            latest = matching[0]
            rows.append(
                {
                    "job_id": job_id,
                    "job_title": job_map[job_id].title or f"Job #{job_id}",
                    "count": len(matching),
                    "latest_headline": latest.headline or latest.suggested_filename or "Sem headline",
                    "updated_at": latest.created_at,
                }
            )
        return rows[:5]

    return {
        "ready_jobs": _build_rows("ready"),
        "published_jobs": _build_rows("published"),
        "discarded_jobs": _build_rows("discarded"),
    }


def enrich_candidates_for_view(
    candidates: list[dict],
    mode: str,
    feedback_profile: dict | None = None,
) -> list[dict]:
    enriched = []
    hybrid_weight_profile = (feedback_profile or {}).get("hybrid_weight_profile", {}) or {}
    preferred_source = hybrid_weight_profile.get("preferred_source", "balanced")
    heuristic_weight = round(float(hybrid_weight_profile.get("heuristic_weight", 0.65) or 0.65), 2)
    llm_weight = round(float(hybrid_weight_profile.get("llm_weight", 0.35) or 0.35), 2)

    def _build_metric_item(label: str, value: float | None) -> dict:
        numeric_value = round(float(value), 2) if value is not None else None
        if numeric_value is None:
            tone = "neutral"
        elif numeric_value >= 7:
            tone = "strong"
        elif numeric_value >= 1:
            tone = "positive"
        elif numeric_value <= -0.5:
            tone = "negative"
        else:
            tone = "neutral"
        return {
            "label": label,
            "value": numeric_value,
            "tone": tone,
        }

    for candidate in candidates:
        start = float(candidate.get("start", 0))
        end = float(candidate.get("end", 0))
        duration = float(candidate.get("duration", 0))
        score = float(candidate.get("score", 0))
        heuristic_score = float(candidate.get("heuristic_score", score) or score)

        opening_text = candidate.get("opening_text") or candidate.get("text", "")[:180]
        closing_text = candidate.get("closing_text") or ""

        if score >= 10:
            score_label = "muito forte"
        elif score >= 7:
            score_label = "forte"
        elif score >= 4:
            score_label = "medio"
        else:
            score_label = "fraco"

        feedback_alignment_score = float(candidate.get("feedback_alignment_score", 0) or 0)
        if feedback_alignment_score >= 1.2:
            feedback_label = "muito alinhado ao feedback"
        elif feedback_alignment_score >= 0.4:
            feedback_label = "alinhado ao feedback"
        elif feedback_alignment_score <= -0.4:
            feedback_label = "fora do padrão aprovado"
        else:
            feedback_label = None

        transcript_context_score = float(candidate.get("transcript_context_score", 0) or 0)
        context_reasons = []
        reason_text = (candidate.get("reason") or "").lower()
        if "tópicos prioritários da transcrição" in reason_text or "topicos prioritarios da transcricao" in reason_text:
            context_reasons.append("alinhado aos tópicos prioritários")
        if "trecho promissor da análise global" in reason_text or "trecho promissor da analise global" in reason_text:
            context_reasons.append("coincide com trecho promissor")
        if "padrão a evitar da transcrição" in reason_text or "padrao a evitar da transcricao" in reason_text:
            context_reasons.append("bate em padrão a evitar")

        if transcript_context_score >= 1.2:
            transcript_context_label = "muito alinhado ao contexto global"
        elif transcript_context_score > 0:
            transcript_context_label = "alinhado ao contexto global"
        elif transcript_context_score <= -0.8:
            transcript_context_label = "desalinhado do contexto global"
        else:
            transcript_context_label = None

        llm_score = candidate.get("llm_score")
        llm_score = round(float(llm_score), 2) if llm_score is not None else None
        if llm_score is not None and llm_score >= 8.5:
            llm_label = "LLM muito confiante"
        elif llm_score is not None and llm_score >= 7.0:
            llm_label = "LLM aprovou bem"
        elif llm_score is not None:
            llm_label = "LLM com ressalvas"
        else:
            llm_label = None

        divergence_score = None
        divergence_label = None
        divergence_summary = None
        if llm_score is not None:
            divergence_score = round(abs(heuristic_score - llm_score), 2)
            if divergence_score >= 2.2:
                divergence_label = "divergência forte"
            elif divergence_score >= 1.2:
                divergence_label = "divergência moderada"

            if divergence_label:
                if llm_score > heuristic_score:
                    divergence_summary = "LLM gostou mais do corte do que o heurístico"
                elif heuristic_score > llm_score:
                    divergence_summary = "Heurístico gostou mais do corte do que a LLM"
                else:
                    divergence_summary = "Heurístico e LLM quase empatados"

        adaptive_blend_explanation = None
        if llm_score is not None:
            if preferred_source == "heuristic" and divergence_score and divergence_score >= 1.2:
                adaptive_blend_explanation = (
                    f"Este corte subiu com mais apoio da heurística porque, neste nicho, "
                    f"divergências recentes estão favorecendo o heurístico ({heuristic_weight} vs {llm_weight})."
                )
            elif preferred_source == "llm" and divergence_score and divergence_score >= 1.2:
                adaptive_blend_explanation = (
                    f"Este corte recebeu mais peso da LLM porque, neste nicho, "
                    f"divergências recentes estão favorecendo a revisão da LLM ({llm_weight} vs {heuristic_weight})."
                )
            elif preferred_source == "balanced" and divergence_score and divergence_score >= 1.2:
                adaptive_blend_explanation = (
                    f"Este corte ficou equilibrado porque o nicho ainda mantém pesos híbridos estáveis "
                    f"({heuristic_weight} heurístico / {llm_weight} LLM)."
                )

        enriched.append(
            {
                **candidate,
                "candidate_id": candidate.get("candidate_id"),
                "status": candidate.get("status", "pending"),
                "is_favorite": bool(candidate.get("is_favorite", False)),
                "editorial_notes": candidate.get("editorial_notes") or "",
                "start_mmss": format_seconds_to_mmss(start),
                "end_mmss": format_seconds_to_mmss(end),
                "duration_mmss": format_seconds_to_mmss(duration),
                "time_range_label": f"{format_seconds_to_mmss(start)} -> {format_seconds_to_mmss(end)}",
                "format_label": "9:16" if mode == "short" else "16:9",
                "opening_preview": opening_text[:220],
                "closing_preview": closing_text[:220],
                "score_label": score_label,
                "heuristic_score": round(heuristic_score, 2),
                "feedback_alignment_score": round(feedback_alignment_score, 2),
                "feedback_label": feedback_label,
                "transcript_context_score": round(transcript_context_score, 2),
                "transcript_context_label": transcript_context_label,
                "transcript_context_reasons": context_reasons,
                "llm_score": llm_score,
                "llm_label": llm_label,
                "llm_why": candidate.get("llm_why") or "",
                "llm_title": candidate.get("llm_title") or "",
                "llm_hook": candidate.get("llm_hook") or "",
                "divergence_score": divergence_score,
                "divergence_label": divergence_label,
                "divergence_summary": divergence_summary,
                "adaptive_blend_explanation": adaptive_blend_explanation,
                "score_breakdown": [
                    _build_metric_item("Final", score),
                    _build_metric_item("Heurístico", heuristic_score),
                    _build_metric_item("Contexto", transcript_context_score),
                    _build_metric_item("LLM", llm_score),
                ],
            }
        )

    return enriched


def sort_candidates_for_view(candidates: list[dict], candidate_sort: str) -> list[dict]:
    normalized = (candidate_sort or "hybrid").strip().lower()
    if normalized == "divergent":
        return sorted(
            candidates,
            key=lambda item: (
                item.get("divergence_score") is not None,
                item.get("divergence_score") or -1,
                item.get("score", 0),
            ),
            reverse=True,
        )
    if normalized == "heuristic":
        return sorted(
            candidates,
            key=lambda item: (
                item.get("heuristic_score", 0),
                item.get("score", 0),
                item.get("llm_score") or -1,
            ),
            reverse=True,
        )
    if normalized == "llm":
        return sorted(
            candidates,
            key=lambda item: (
                item.get("llm_score") is not None,
                item.get("llm_score") or -1,
                item.get("score", 0),
            ),
            reverse=True,
        )
    return sorted(
        candidates,
        key=lambda item: (
            item.get("score", 0),
            item.get("heuristic_score", 0),
            item.get("llm_score") or -1,
        ),
        reverse=True,
    )


def enrich_clips_for_view(clips: list[Clip]) -> list[dict]:
    enriched = []

    for clip in clips:
        base_payload = serialize_clip(clip)
        enriched.append(
            {
                **base_payload,
                "format_label": "9:16" if clip.mode == "short" else "16:9",
                "publication_status_label": PUBLICATION_STATUS_LABELS.get(
                    clip.publication_status,
                    clip.publication_status or "Rascunho",
                ),
                "start_mmss": format_seconds_to_mmss(clip.start_time),
                "end_mmss": format_seconds_to_mmss(clip.end_time),
                "duration_mmss": format_seconds_to_mmss(clip.duration),
            }
        )

    return enriched


def enrich_steps_for_view(steps: list) -> list[dict]:
    enriched = []

    for step in steps:
        status = step.status or "pending"
        if status == "completed":
            status_label = "Concluida"
        elif status == "skipped":
            status_label = "Pulada"
        elif status == "running":
            status_label = "Executando"
        elif status == "failed":
            status_label = "Falhou"
        elif status == "exhausted":
            status_label = "Tentativas esgotadas"
        else:
            status_label = "Pendente"
        if step.step_name == "llm_enrichment":
            if status == "completed":
                status_label = "Enriquecimento concluido"
            elif status == "running":
                status_label = "Enriquecendo com LLM"
            elif status == "skipped":
                status_label = "LLM pulada"

        try:
            details_payload = json.loads(step.details) if step.details else {}
        except json.JSONDecodeError:
            details_payload = {"raw_details": step.details}

        if not isinstance(details_payload, dict):
            details_payload = {"value": details_payload}

        duration_seconds = details_payload.get("duration_seconds")
        duration_label = (
            f"{float(duration_seconds):.3f}s"
            if isinstance(duration_seconds, (int, float))
            else None
        )
        heartbeat_at = details_payload.get("heartbeat_at")
        progress_message = details_payload.get("progress_message")
        progress_percent = details_payload.get("progress_percent")
        heartbeat_age_seconds = None
        heartbeat_is_stale = False
        if heartbeat_at and status == "running":
            try:
                heartbeat_dt = datetime.fromisoformat(str(heartbeat_at).replace("Z", "+00:00"))
                now = datetime.now(heartbeat_dt.tzinfo) if heartbeat_dt.tzinfo else datetime.utcnow()
                heartbeat_age_seconds = max(0, round((now - heartbeat_dt).total_seconds()))
                heartbeat_is_stale = heartbeat_age_seconds >= STEP_STALE_HEARTBEAT_SECONDS
            except ValueError:
                heartbeat_age_seconds = None
                heartbeat_is_stale = False

        summary_items = []
        reason = details_payload.get("reason")
        if reason:
            summary_items.append(f"Motivo: {reason}")
        attempt = details_payload.get("attempt")
        if attempt is not None:
            summary_items.append(f"Tentativa registrada: {attempt}")
        if duration_label:
            summary_items.append(f"Duração: {duration_label}")
        if details_payload.get("forced") is True:
            summary_items.append("Execução forçada")

        if progress_message and status == "running":
            summary_items.append(f"Atividade: {progress_message}")
        if heartbeat_at and status == "running":
            summary_items.append(f"Ultima atividade: {heartbeat_at}")
        if heartbeat_is_stale:
            summary_items.append("Possivel travamento detectado")

        detail_items = []
        for key, value in details_payload.items():
            if key in {"reason", "attempt", "duration_seconds", "forced", "progress_message", "heartbeat_at", "progress_percent"}:
                continue
            if value in (None, "", [], {}):
                continue
            detail_items.append({"label": key.replace("_", " "), "value": value})

        enriched.append(
            {
                "id": step.id,
                "step_name": step.step_name,
                "status": status,
                "status_label": status_label,
                "attempts": step.attempts or 0,
                "max_attempts": MAX_STEP_ATTEMPTS,
                "error_message": step.error_message,
                "details": step.details,
                "details_payload": details_payload,
                "detail_items": detail_items,
                "summary_items": summary_items,
                "duration_seconds": duration_seconds,
                "duration_label": duration_label,
                "progress_message": progress_message,
                "progress_percent": progress_percent,
                "heartbeat_at": heartbeat_at,
                "heartbeat_age_seconds": heartbeat_age_seconds,
                "heartbeat_is_stale": heartbeat_is_stale,
                "started_at": step.started_at,
                "completed_at": step.completed_at,
                "can_retry": status in {"failed", "pending"},
                "can_force_retry": status == "exhausted",
                "can_reset": True,
            }
        )

    return enriched


def enrich_feedback_profile_for_view(profile: dict | None) -> dict | None:
    if not profile:
        return None

    successful_keywords = profile.get("successful_keywords", [])[:6]
    hybrid_weight_profile = profile.get("hybrid_weight_profile", {}) or {}
    preferred_source = hybrid_weight_profile.get("preferred_source", "balanced")
    if preferred_source == "heuristic":
        hybrid_summary = "Quando há divergência, este nicho está favorecendo mais a heurística."
    elif preferred_source == "llm":
        hybrid_summary = "Quando há divergência, este nicho está favorecendo mais a revisão da LLM."
    else:
        hybrid_summary = "Quando há divergência, o sistema ainda está equilibrado entre heurística e LLM."

    return {
        **profile,
        "is_ready": bool(profile.get("min_samples_reached")),
        "positive_count": profile.get("positive_count", 0),
        "negative_count": profile.get("negative_count", 0),
        "sample_count": profile.get("sample_count", 0),
        "successful_keywords_preview": successful_keywords,
        "hybrid_weight_profile": {
            **hybrid_weight_profile,
            "heuristic_weight": round(float(hybrid_weight_profile.get("heuristic_weight", 0.65) or 0.65), 2),
            "llm_weight": round(float(hybrid_weight_profile.get("llm_weight", 0.35) or 0.35), 2),
            "reviewed_count": int(hybrid_weight_profile.get("reviewed_count", 0) or 0),
            "approved_count": int(hybrid_weight_profile.get("approved_count", 0) or 0),
            "rejected_count": int(hybrid_weight_profile.get("rejected_count", 0) or 0),
            "preferred_source": preferred_source,
            "summary": hybrid_summary,
        },
    }


def enrich_transcript_insights_for_view(raw_insights: str | None) -> dict | None:
    if not raw_insights:
        return None

    try:
        parsed = json.loads(raw_insights)
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed, dict):
        return None

    promising_ranges = []
    for item in parsed.get("promising_ranges", [])[:6]:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start_hint_seconds", 0) or 0)
            end = float(item.get("end_hint_seconds", 0) or 0)
        except (TypeError, ValueError):
            continue
        promising_ranges.append(
            {
                "start": start,
                "end": end,
                "label": f"{format_seconds_to_mmss(start)} -> {format_seconds_to_mmss(end)}",
                "why": item.get("why") or "",
            }
        )

    return {
        "main_topics": parsed.get("main_topics", [])[:6],
        "viral_angles": parsed.get("viral_angles", [])[:6],
        "priority_keywords": parsed.get("priority_keywords", [])[:8],
        "avoid_patterns": parsed.get("avoid_patterns", [])[:8],
        "promising_ranges": promising_ranges,
    }


@router.get("/")
def public_home(
    request: Request,
    workspace: Workspace | None = Depends(get_current_workspace),
):
    if workspace is not None:
        query_string = request.url.query
        dashboard_url = "/dashboard"
        if query_string:
            dashboard_url = f"{dashboard_url}?{query_string}"
        return RedirectResponse(url=dashboard_url, status_code=303)
    return templates.TemplateResponse(request, "index.html", {})


@router.get("/dashboard")
def dashboard(
    request: Request,
    status_filter: str = "all",
    search_query: str = "",
    message: str | None = None,
    message_level: str = "success",
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    recent_jobs = (
        db.query(Job)
        .filter(Job.workspace_id == workspace.id)
        .order_by(Job.created_at.desc())
        .limit(20)
        .all()
    )
    if not recent_jobs and not message:
        return RedirectResponse(url="/onboarding", status_code=303)
    recent_jobs = enrich_jobs_with_progress(db, recent_jobs)
    filtered_jobs = filter_jobs_for_view(recent_jobs, status_filter)
    filtered_jobs = search_jobs_for_view(filtered_jobs, search_query)
    dashboard_summary = build_dashboard_summary(db, recent_jobs)
    pipeline_health = build_pipeline_health_summary(db, recent_jobs)
    priority_groups = build_job_priority_groups(db, filtered_jobs)
    publication_board = build_publication_board(db, recent_jobs)
    quota_status = get_workspace_quota_status(db, workspace.id)
    runtime_readiness = build_runtime_readiness()

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "recent_jobs": filtered_jobs,
            "status_filter": status_filter,
            "search_query": search_query,
            "dashboard_summary": dashboard_summary,
            "pipeline_health": pipeline_health,
            "priority_groups": priority_groups,
            "publication_board": publication_board,
            "quota_status": quota_status,
            "runtime_readiness": runtime_readiness,
            "flash": {"message": message, "level": message_level} if message else None,
            "now": datetime.now(),
            "auto_refresh": has_active_jobs(filtered_jobs),
        },
    )


@router.get("/onboarding")
def onboarding(
    request: Request,
    message: str | None = None,
    message_level: str = "success",
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    first_job = (
        db.query(Job)
        .filter(Job.workspace_id == workspace.id)
        .order_by(Job.created_at.asc())
        .first()
    )
    if first_job:
        return RedirectResponse(url=_job_view_url(first_job.id), status_code=303)

    quota_status = get_workspace_quota_status(db, workspace.id)
    return templates.TemplateResponse(
        request,
        "onboarding.html",
        {
            "quota_status": quota_status,
            "flash": {"message": message, "level": message_level} if message else None,
        },
    )


@router.get("/nichos")
def niche_admin_page(
    request: Request,
    message: str | None = None,
    level: str | None = None,
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    niches = list_niche_definitions(db, include_inactive=True, workspace_id=workspace.id)
    active_niches = [niche for niche in niches if niche["status"] == "active"]
    pending_niches = [niche for niche in niches if niche["status"] == "pending"]
    inactive_niches = [niche for niche in niches if niche["status"] in {"archived", "rejected"}]

    return templates.TemplateResponse(
        request,
        "nicho.html",
        {
            "active_niches": active_niches,
            "pending_niches": pending_niches,
            "inactive_niches": inactive_niches,
            "flash": _build_niche_flash(message, level),
        },
    )


@router.get("/system")
def system_status_page(request: Request):
    diagnostics = build_system_diagnostics()
    return templates.TemplateResponse(
        request,
        "system.html",
        {
            "diagnostics": diagnostics,
        },
    )


@router.post("/nichos/sugerir")
def suggest_niche_from_page(
    name: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    try:
        create_pending_niche(
            db,
            name=name,
            description=description.strip() or None,
            workspace_id=workspace.id,
        )
    except ValueError as exc:
        return _niche_redirect(str(exc), "warning")
    except Exception as exc:
        return _niche_redirect(f"Não foi possível gerar a sugestão do nicho: {str(exc)}", "error")

    return _niche_redirect(
        "Sugestão gerada. Revise as palavras-chave e aprove manualmente.",
        "success",
    )


@router.post("/nichos/{slug}/aprovar")
def approve_niche_from_page(
    slug: str,
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    try:
        approve_niche(db, slug, workspace_id=workspace.id)
    except ValueError as exc:
        return _niche_redirect(str(exc), "warning")
    return _niche_redirect("Nicho aprovado e ativado no motor heurístico.", "success")


@router.post("/nichos/{slug}/rejeitar")
def reject_niche_from_page(
    slug: str,
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    try:
        reject_niche(db, slug, workspace_id=workspace.id)
    except ValueError as exc:
        return _niche_redirect(str(exc), "warning")
    return _niche_redirect("Sugestão de nicho rejeitada.", "success")


@router.post("/nichos/{slug}/excluir")
def archive_niche_from_page(
    slug: str,
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    try:
        archive_niche(db, slug, workspace_id=workspace.id)
    except ValueError as exc:
        return _niche_redirect(str(exc), "warning")
    return _niche_redirect("Nicho removido da lista ativa.", "success")


@router.post("/web/jobs/create")
def create_job_from_form(
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    try:
        metadata = fetch_youtube_metadata(url)
        ensure_workspace_can_start_job(db, workspace.id)
        ensure_workspace_can_create_job(
            db,
            workspace.id,
            duration_seconds=float(metadata.get("duration_seconds") or 0.0),
        )
    except HTTPException as exc:
        target_url = "/billing" if _is_billing_activation_message(str(exc.detail)) else "/onboarding"
        return RedirectResponse(
            url=f"{target_url}?{urlencode({'message': str(exc.detail), 'level': 'warning'})}",
            status_code=303,
        )
    except RuntimeError as exc:
        return RedirectResponse(
            url=f"/onboarding?{urlencode({'message': str(exc), 'level': 'error'})}",
            status_code=303,
        )
    job = Job(
        workspace_id=workspace.id,
        source_type="youtube",
        source_value=url,
        status="pending",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    enqueue_pipeline_job(background_tasks, job.id)

    return RedirectResponse(url=_job_view_url(job.id), status_code=303)


@router.post("/web/jobs/create-local")
def create_local_job_from_form(
    background_tasks: BackgroundTasks,
    video_file: UploadFile = File(...),
    title: str = Form(""),
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    if not video_file.filename:
        raise HTTPException(status_code=400, detail="Arquivo de video nao informado")

    original_name = Path(video_file.filename).name
    suffix = Path(original_name).suffix.lower()
    if suffix not in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}:
        raise HTTPException(status_code=400, detail="Formato de video nao suportado")

    stored_path = get_storage().path_for(
        normalize_storage_key("uploads", f"{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}_{original_name}")
    )

    with stored_path.open("wb") as buffer:
        shutil.copyfileobj(video_file.file, buffer)

    get_storage().sync_path(stored_path)
    try:
        ensure_workspace_can_start_job(db, workspace.id)
        ensure_workspace_can_create_job(
            db,
            workspace.id,
            duration_seconds=probe_video_duration_seconds(stored_path),
        )
    except HTTPException as exc:
        target_url = "/billing" if _is_billing_activation_message(str(exc.detail)) else "/onboarding"
        return RedirectResponse(
            url=f"{target_url}?{urlencode({'message': str(exc.detail), 'level': 'warning'})}",
            status_code=303,
        )
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        return RedirectResponse(
            url=f"/onboarding?{urlencode({'message': str(exc), 'level': 'error'})}",
            status_code=303,
        )

    resolved_title = title.strip() or Path(original_name).stem
    job = Job(
        workspace_id=workspace.id,
        source_type="local",
        source_value=str(stored_path),
        status="pending",
        title=resolved_title,
        video_path=str(stored_path),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    enqueue_pipeline_job(background_tasks, job.id)

    video_file.file.close()

    return RedirectResponse(url=_job_view_url(job.id), status_code=303)


@router.post("/jobs/{job_id}/view/retry")
def retry_job_from_page(
    job_id: int,
    force: str | None = Form(None),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    force_bool = force is not None
    job.status = "pending"
    job.error_message = None
    db.commit()

    enqueue_pipeline_job(background_tasks, job.id, force=force_bool)
    return RedirectResponse(url=_job_view_url(job.id), status_code=303)


@router.post("/jobs/{job_id}/view/steps/{step_name}/retry")
def retry_job_step_from_page(
    job_id: int,
    step_name: str,
    force: str | None = Form(None),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    force_bool = force is not None
    from app.services.pipeline import reset_pipeline_state_from_step, validate_step_name

    normalized_step = validate_step_name(step_name)
    reset_pipeline_state_from_step(db, job, normalized_step, reset_attempts=False)
    enqueue_pipeline_job(background_tasks, job.id, force=force_bool, start_step=normalized_step)

    return RedirectResponse(url=_job_view_url(job.id), status_code=303)


@router.post("/jobs/{job_id}/view/steps/{step_name}/reset")
def reset_job_step_from_page(
    job_id: int,
    step_name: str,
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    from app.services.pipeline import reset_pipeline_state_from_step, validate_step_name

    normalized_step = validate_step_name(step_name)
    reset_pipeline_state_from_step(db, job, normalized_step, reset_attempts=True)

    return RedirectResponse(url=_job_view_url(job.id), status_code=303)


@router.post("/jobs/{job_id}/view/feedback/recalibrate")
def recalibrate_feedback_from_page(
    job_id: int,
    mode: str = Form("short"),
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)

    normalized_mode = _normalize_mode(mode)
    learn_keywords_for_niche(db, niche=(job.detected_niche or "geral").lower().strip())

    return RedirectResponse(
        url=_job_view_url(job.id, mode=normalized_mode, message="Aprendizado recalibrado."),
        status_code=303,
    )


@router.post("/jobs/{job_id}/view/analyze-without-llm")
def analyze_without_llm_from_page(
    job_id: int,
    mode: str = Form("short"),
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail="Job nÃ£o encontrado")
    if not job.video_path or not job.transcript_path:
        raise HTTPException(status_code=400, detail="Job precisa de video e transcricao para concluir a analise sem LLM")

    normalized_mode = _normalize_mode(mode)
    complete_analysis_without_llm(db, job, force=True)

    return RedirectResponse(
        url=_job_view_url(
            job.id,
            mode=normalized_mode,
            message="Analise concluida sem LLM.",
        ),
        status_code=303,
    )


@router.post("/jobs/{job_id}/view/cancel")
def cancel_job_from_page(
    job_id: int,
    mode: str = Form("short"),
    render_preset: str = Form(DEFAULT_PRESET),
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    request_job_cancellation(db, job)
    normalized_mode = _normalize_mode(mode)
    return RedirectResponse(
        url=_job_view_url(
            job.id,
            mode=normalized_mode,
            render_preset=render_preset,
            message="Cancelamento solicitado. O worker vai encerrar na proxima verificacao segura.",
            level="warning",
        ),
        status_code=303,
    )


@router.get("/jobs/{job_id}/view")
def job_detail(
    job_id: int,
    request: Request,
    mode: str = "short",
    render_preset: str = DEFAULT_PRESET,
    message: str | None = None,
    message_level: str = "success",
    candidate_filter: str = "all",
    candidate_sort: str = "hybrid",
    clip_filter: str = "all",
    export_filter: str = "all",
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    job = db.query(Job).filter(Job.id == job_id, Job.workspace_id == workspace.id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    normalized_mode = _normalize_mode(mode)
    candidates = []
    feedback_profile = None
    candidates_missing = False
    transcript_insights = enrich_transcript_insights_for_view(job.transcript_insights)
    candidates_total_count = 0
    if job.transcript_path and job.status in {"analyzing", "llm_enrichment", "done"}:
        feedback_profile = get_feedback_profile_for_niche(db, job.detected_niche or "geral", normalized_mode, workspace_id=job.workspace_id)
        saved_candidates = _get_candidates_for_job_view(db, job, normalized_mode)
        candidates_total_count = len(saved_candidates)
        candidates_missing = not bool(saved_candidates)
        if saved_candidates:
            candidates = enrich_candidates_for_view(
                [serialize_candidate(candidate) for candidate in saved_candidates],
                mode=normalized_mode,
                feedback_profile=feedback_profile,
            )
            if candidate_filter == "approved":
                candidates = [candidate for candidate in candidates if candidate["status"] == "approved"]
            elif candidate_filter == "rejected":
                candidates = [candidate for candidate in candidates if candidate["status"] == "rejected"]
            elif candidate_filter == "rendered":
                candidates = [candidate for candidate in candidates if candidate["status"] == "rendered"]
            elif candidate_filter == "favorite":
                candidates = [candidate for candidate in candidates if candidate["is_favorite"]]
            elif candidate_filter == "divergent":
                candidates = [candidate for candidate in candidates if candidate.get("divergence_label")]
            candidates = sort_candidates_for_view(candidates, candidate_sort)

    clips = (
        db.query(Clip)
        .filter(Clip.job_id == job_id)
        .order_by(Clip.created_at.desc())
        .all()
    )
    if clip_filter == "short":
        clips = [clip for clip in clips if clip.mode == "short"]
    elif clip_filter == "long":
        clips = [clip for clip in clips if clip.mode == "long"]
    elif clip_filter == "subtitled":
        clips = [clip for clip in clips if clip.subtitles_burned]
    elif clip_filter == "ready":
        clips = [clip for clip in clips if clip.publication_status == "ready"]
    elif clip_filter == "published":
        clips = [clip for clip in clips if clip.publication_status == "published"]

    exports = list_job_export_bundles(job.id)
    if export_filter == "latest":
        exports = exports[:1]
    steps = get_job_steps(db, job.id)
    queue_waiting = job.status == "pending" and (job.error_message or "").startswith("Aguardando vaga na fila")

    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            "job": job,
            "mode": normalized_mode,
            "render_preset": render_preset,
            "candidate_filter": candidate_filter,
            "candidate_sort": candidate_sort,
            "candidates_total_count": candidates_total_count,
            "clip_filter": clip_filter,
            "export_filter": export_filter,
            "render_presets": list_render_presets(),
            "candidates": candidates,
            "clips": enrich_clips_for_view(clips),
            "exports": exports,
            "steps": enrich_steps_for_view(steps),
            "queue_waiting": queue_waiting,
            "feedback_profile": enrich_feedback_profile_for_view(feedback_profile),
            "candidates_missing": candidates_missing,
            "transcript_insights": transcript_insights,
            "video_url": build_static_url(job.video_path),
            "audio_url": build_static_url(job.audio_path),
            "transcript_url": build_static_url(job.transcript_path),
            "auto_refresh_enabled": job.status in JOB_AUTO_REFRESH_STATUSES,
            "auto_refresh_interval_ms": JOB_AUTO_REFRESH_INTERVAL_MS,
            "flash": {"message": message, "level": message_level} if message else None,
            "build_static_url": build_static_url,
        },
    )


@router.get("/account")
def account_profile_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_current_user),
    workspace: Workspace = Depends(require_current_workspace),
):
    membership = (
        db.query(WorkspaceMember)
        .filter(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == current_user.id,
            WorkspaceMember.status == "active",
        )
        .first()
    )
    jobs_count = db.query(Job).filter(Job.workspace_id == workspace.id).count()
    rendered_clips_count = (
        db.query(Clip)
        .join(Job, Clip.job_id == Job.id)
        .filter(Job.workspace_id == workspace.id)
        .count()
    )
    approved_candidates_count = (
        db.query(Candidate)
        .join(Job, Candidate.job_id == Job.id)
        .filter(Job.workspace_id == workspace.id, Candidate.status == "approved")
        .count()
    )
    quota_status = get_workspace_quota_status(db, workspace.id)

    return templates.TemplateResponse(
        request,
        "account.html",
        {
            "user": current_user,
            "workspace": workspace,
            "membership": membership,
            "jobs_count": jobs_count,
            "rendered_clips_count": rendered_clips_count,
            "approved_candidates_count": approved_candidates_count,
            "quota_status": quota_status,
        },
    )


@router.post("/jobs/{job_id}/view/render-candidate")
def render_candidate_from_page(
    job_id: int,
    candidate_id: int = Form(...),
    mode: str = Form(...),
    render_preset: str = Form(DEFAULT_PRESET),
    burn_subtitles: str | None = Form(None),
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")
    if not job.video_path:
        raise HTTPException(status_code=400, detail="Job incompleto")

    normalized_mode = _normalize_mode(mode)
    burn_subtitles_bool = burn_subtitles is not None
    if candidate_id <= 0:
        raise HTTPException(status_code=400, detail="candidate_id inválido")

    candidate = _get_candidate_or_404(db, candidate_id)
    if candidate.job_id != job.id or candidate.mode != normalized_mode:
        raise HTTPException(status_code=400, detail="Candidato não pertence ao job/modo informado")

    clip, _subtitles_path, _output_path = render_candidate_clip(
        db=db,
        job=job,
        candidate=candidate,
        burn_subtitles=burn_subtitles_bool,
        render_preset=render_preset,
    )
    db.commit()

    return RedirectResponse(
        url=_job_view_url(
            job.id,
            mode=normalized_mode,
            render_preset=render_preset,
            message="Render concluido com sucesso.",
        ),
        status_code=303,
    )


@router.post("/jobs/{job_id}/view/candidates/{candidate_id}/status")
def update_candidate_status_from_page(
    job_id: int,
    candidate_id: int,
    mode: str = Form("short"),
    status: str = Form(...),
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    candidate = _get_candidate_or_404(db, candidate_id)
    if candidate.job_id != job.id:
        raise HTTPException(status_code=400, detail="Candidato não pertence ao job informado")

    allowed_statuses = {"pending", "approved", "rejected"}
    normalized_status = status.lower().strip()
    if normalized_status not in allowed_statuses:
        raise HTTPException(status_code=400, detail="Status editorial inválido")

    candidate.status = normalized_status
    db.commit()

    return RedirectResponse(
        url=_job_view_url(job.id, mode=mode, message="Atualizacao salva."),
        status_code=303,
    )


@router.post("/jobs/{job_id}/view/candidates/{candidate_id}/favorite")
def toggle_candidate_favorite_from_page(
    job_id: int,
    candidate_id: int,
    mode: str = Form("short"),
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    candidate = _get_candidate_or_404(db, candidate_id)
    if candidate.job_id != job.id:
        raise HTTPException(status_code=400, detail="Candidato não pertence ao job informado")

    candidate.is_favorite = not bool(candidate.is_favorite)
    db.commit()

    return RedirectResponse(
        url=_job_view_url(job.id, mode=mode, message="Atualizacao salva."),
        status_code=303,
    )


@router.post("/jobs/{job_id}/view/candidates/{candidate_id}/notes")
def update_candidate_notes_from_page(
    job_id: int,
    candidate_id: int,
    mode: str = Form("short"),
    editorial_notes: str = Form(""),
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    candidate = _get_candidate_or_404(db, candidate_id)
    if candidate.job_id != job.id:
        raise HTTPException(status_code=400, detail="Candidato não pertence ao job informado")

    candidate.editorial_notes = editorial_notes.strip() or None
    db.commit()

    return RedirectResponse(
        url=_job_view_url(job.id, mode=mode, message="Atualizacao salva."),
        status_code=303,
    )


@router.post("/jobs/{job_id}/view/candidates/bulk")
def bulk_update_candidates_from_page(
    job_id: int,
    mode: str = Form("short"),
    bulk_action: str = Form(...),
    candidate_ids: list[int] = Form([]),
    render_preset: str = Form(DEFAULT_PRESET),
    burn_subtitles: str | None = Form(None),
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)

    normalized_mode = _normalize_mode(mode)
    normalized_action = (bulk_action or "").strip().lower()
    selected_ids = [int(candidate_id) for candidate_id in candidate_ids if int(candidate_id) > 0]
    if not selected_ids:
        return RedirectResponse(
            url=_job_view_url(job.id, mode=normalized_mode, render_preset=render_preset, message="Selecione ao menos um candidato.", level="error"),
            status_code=303,
        )

    candidates = (
        db.query(Candidate)
        .filter(Candidate.job_id == job.id, Candidate.mode == normalized_mode, Candidate.id.in_(selected_ids))
        .all()
    )
    if len(candidates) != len(set(selected_ids)):
        return RedirectResponse(
            url=_job_view_url(job.id, mode=normalized_mode, render_preset=render_preset, message="Alguns candidatos selecionados nao pertencem ao job ou modo atual.", level="error"),
            status_code=303,
        )

    if normalized_action in {"approve", "reject", "reset"}:
        target_status = {"approve": "approved", "reject": "rejected", "reset": "pending"}[normalized_action]
        for candidate in candidates:
            candidate.status = target_status
        db.commit()
        return RedirectResponse(
            url=_job_view_url(job.id, mode=normalized_mode, render_preset=render_preset, message="Candidatos atualizados em lote."),
            status_code=303,
        )

    if normalized_action in {"favorite_on", "favorite_off"}:
        favorite_value = normalized_action == "favorite_on"
        for candidate in candidates:
            candidate.is_favorite = favorite_value
        db.commit()
        return RedirectResponse(
            url=_job_view_url(job.id, mode=normalized_mode, render_preset=render_preset, message="Favoritos atualizados em lote."),
            status_code=303,
        )

    if normalized_action == "render":
        if not job.video_path:
            raise HTTPException(status_code=400, detail="Job incompleto")
        subtitles_requested = burn_subtitles is not None
        burn_subtitles_bool = subtitles_requested and bool(job.transcript_path)
        for candidate in sorted(candidates, key=lambda row: (not bool(row.is_favorite), -(row.score or 0), row.created_at)):
            render_candidate_clip(
                db=db,
                job=job,
                candidate=candidate,
                burn_subtitles=burn_subtitles_bool,
                render_preset=render_preset,
            )
        db.commit()
        flash_message = "Selecao renderizada com sucesso."
        flash_level = "success"
        if subtitles_requested and not job.transcript_path:
            flash_message = "Selecao renderizada sem legenda embutida porque este job ainda nao possui transcricao."
            flash_level = "warning"
        return RedirectResponse(
            url=_job_view_url(job.id, mode=normalized_mode, render_preset=render_preset, message=flash_message, level=flash_level),
            status_code=303,
        )

    return RedirectResponse(
        url=_job_view_url(job.id, mode=normalized_mode, render_preset=render_preset, message="Acao em lote invalida.", level="error"),
        status_code=303,
    )


@router.post("/jobs/{job_id}/view/clips/{clip_id}/publication")
def update_clip_publication_status_from_page(
    job_id: int,
    clip_id: int,
    mode: str = Form("short"),
    render_preset: str = Form(DEFAULT_PRESET),
    status: str = Form(...),
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    clip = db.query(Clip).filter(Clip.id == clip_id, Clip.job_id == job.id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clip não encontrado")

    try:
        normalized_status = normalize_publication_status(status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    clip.publication_status = normalized_status
    db.commit()

    return RedirectResponse(
        url=_job_view_url(
            job.id,
            mode=mode,
            render_preset=render_preset,
            message="Status de publicacao atualizado.",
        ),
        status_code=303,
    )


@router.post("/jobs/{job_id}/view/render-approved")
def render_approved_from_page(
    job_id: int,
    mode: str = Form("short"),
    render_preset: str = Form(DEFAULT_PRESET),
    burn_subtitles: str | None = Form(None),
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")
    if not job.video_path:
        raise HTTPException(status_code=400, detail="Job incompleto")

    normalized_mode = _normalize_mode(mode)
    subtitles_requested = burn_subtitles is not None
    burn_subtitles_bool = subtitles_requested and bool(job.transcript_path)
    approved_candidates = (
        db.query(Candidate)
        .filter(
            Candidate.job_id == job.id,
            Candidate.mode == normalized_mode,
            Candidate.status == "approved",
        )
        .order_by(Candidate.is_favorite.desc(), Candidate.score.desc(), Candidate.created_at.asc())
        .all()
    )

    for candidate in approved_candidates:
        render_candidate_clip(
            db=db,
            job=job,
            candidate=candidate,
            burn_subtitles=burn_subtitles_bool,
            render_preset=render_preset,
        )

    db.commit()

    flash_message = "Render concluido com sucesso."
    flash_level = "success"
    if subtitles_requested and not job.transcript_path:
        flash_message = "Render concluido sem legenda embutida porque este job ainda nao possui transcricao."
        flash_level = "warning"

    return RedirectResponse(
        url=_job_view_url(
            job.id,
            mode=normalized_mode,
            render_preset=render_preset,
            message=flash_message,
            level=flash_level,
        ),
        status_code=303,
    )


@router.post("/jobs/{job_id}/view/render-manual")
def render_manual_from_page(
    job_id: int,
    start: str = Form(""),
    end: str = Form(""),
    start_hours: str = Form(""),
    start_minutes: str = Form(""),
    start_seconds: str = Form(""),
    end_hours: str = Form(""),
    end_minutes: str = Form(""),
    end_seconds: str = Form(""),
    mode: str = Form(...),
    render_preset: str = Form(DEFAULT_PRESET),
    burn_subtitles: str | None = Form(None),
    db: Session = Depends(get_db),
    workspace: Workspace = Depends(require_current_workspace),
):
    job = _get_job_for_workspace_or_404(db, job_id, workspace)
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")
    if not job.video_path:
        raise HTTPException(status_code=400, detail="Job incompleto")

    normalized_mode = _normalize_mode(mode)
    start_value = start.strip() or _build_timecode_from_parts(start_hours, start_minutes, start_seconds)
    end_value = end.strip() or _build_timecode_from_parts(end_hours, end_minutes, end_seconds)
    try:
        start_seconds = parse_timecode_to_seconds(start_value)
        end_seconds = parse_timecode_to_seconds(end_value)
    except ValueError:
        return RedirectResponse(
            url=_job_view_url(
                job.id,
                mode=normalized_mode,
                render_preset=render_preset,
                message="Tempo invalido. Use segundos, mm:ss ou hh:mm:ss.",
                level="error",
            ),
            status_code=303,
        )

    if end_seconds <= start_seconds:
        raise HTTPException(status_code=400, detail="end deve ser maior que start")

    subtitles_requested = burn_subtitles is not None
    burn_subtitles_bool = subtitles_requested and bool(job.transcript_path)

    clip, _subtitles_path, _output_path = render_manual_clip(
        db=db,
        job=job,
        start=start_seconds,
        end=end_seconds,
        mode=normalized_mode,
        burn_subtitles=burn_subtitles_bool,
        render_preset=render_preset,
        clip_index=9999,
        reason="Render manual via interface web",
    )
    db.commit()

    flash_message = "Render concluido com sucesso."
    flash_level = "success"
    if subtitles_requested and not job.transcript_path:
        flash_message = "Render concluido sem legenda embutida porque este job ainda nao possui transcricao."
        flash_level = "warning"

    return RedirectResponse(
        url=_job_view_url(
            job.id,
            mode=normalized_mode,
            render_preset=render_preset,
            message=flash_message,
            level=flash_level,
        ),
        status_code=303,
    )
