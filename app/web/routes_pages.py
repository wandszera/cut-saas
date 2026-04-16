import json
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.clip import Clip
from app.models.job import Job
from app.services.clipping import render_clip
from app.services.niche_learning import (
    get_feedback_profile_for_niche,
    get_learned_keywords_for_niche,
    learn_keywords_for_niche,
)
from app.services.pipeline import MAX_STEP_ATTEMPTS, get_job_steps, process_job_pipeline
from app.services.scoring import score_candidates
from app.services.segmentation import build_candidate_windows, load_segments
from app.services.subtitles import generate_ass_for_clip
from app.utils.media_urls import build_static_url


router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory="app/templates")


def has_active_jobs(jobs: list[Job]) -> bool:
    active_statuses = {
        "pending",
        "downloading",
        "extracting_audio",
        "transcribing",
        "analyzing",
        "rendering",
    }
    return any(job.status in active_statuses for job in jobs)


def _normalize_mode(mode: str) -> str:
    normalized = mode.lower().strip()
    return normalized if normalized in {"short", "long"} else "short"


def _get_ranked_candidates(db: Session, job: Job, mode: str) -> list[dict]:
    raw_segments = load_segments(job.transcript_path)
    candidates = build_candidate_windows(raw_segments, mode=mode)
    niche = job.detected_niche or "geral"
    learned_keywords = get_learned_keywords_for_niche(db, niche)
    feedback_profile = get_feedback_profile_for_niche(db, niche, mode)
    return score_candidates(
        candidates,
        mode=mode,
        niche=niche,
        learned_keywords=learned_keywords,
        feedback_profile=feedback_profile,
    )


def format_seconds_to_mmss(seconds: float | int | None) -> str:
    if seconds is None:
        return "--:--"

    total = int(round(float(seconds)))
    minutes = total // 60
    secs = total % 60
    return f"{minutes:02}:{secs:02}"


def enrich_candidates_for_view(candidates: list[dict], mode: str) -> list[dict]:
    enriched = []

    for candidate in candidates:
        start = float(candidate.get("start", 0))
        end = float(candidate.get("end", 0))
        duration = float(candidate.get("duration", 0))
        score = float(candidate.get("score", 0))

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

        enriched.append(
            {
                **candidate,
                "start_mmss": format_seconds_to_mmss(start),
                "end_mmss": format_seconds_to_mmss(end),
                "duration_mmss": format_seconds_to_mmss(duration),
                "time_range_label": f"{format_seconds_to_mmss(start)} -> {format_seconds_to_mmss(end)}",
                "format_label": "9:16" if mode == "short" else "16:9",
                "opening_preview": opening_text[:220],
                "closing_preview": closing_text[:220],
                "score_label": score_label,
                "feedback_alignment_score": round(feedback_alignment_score, 2),
                "feedback_label": feedback_label,
            }
        )

    return enriched


def enrich_clips_for_view(clips: list[Clip]) -> list[dict]:
    enriched = []

    for clip in clips:
        enriched.append(
            {
                "id": clip.id,
                "job_id": clip.job_id,
                "source": clip.source,
                "mode": clip.mode,
                "start_time": clip.start_time,
                "end_time": clip.end_time,
                "duration": clip.duration,
                "score": clip.score,
                "reason": clip.reason,
                "text": clip.text,
                "subtitles_burned": clip.subtitles_burned,
                "output_path": clip.output_path,
                "created_at": clip.created_at,
                "format_label": "9:16" if clip.mode == "short" else "16:9",
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

        detail_items = []
        for key, value in details_payload.items():
            if key in {"reason", "attempt", "duration_seconds", "forced"}:
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
    return {
        **profile,
        "is_ready": bool(profile.get("min_samples_reached")),
        "positive_count": profile.get("positive_count", 0),
        "negative_count": profile.get("negative_count", 0),
        "sample_count": profile.get("sample_count", 0),
        "successful_keywords_preview": successful_keywords,
    }


@router.get("/")
def home(request: Request, db: Session = Depends(get_db)):
    recent_jobs = db.query(Job).order_by(Job.created_at.desc()).limit(20).all()

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "recent_jobs": recent_jobs,
            "now": datetime.utcnow(),
            "auto_refresh": has_active_jobs(recent_jobs),
        },
    )


@router.post("/web/jobs/create")
def create_job_from_form(
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    db: Session = Depends(get_db),
):
    job = Job(
        source_type="youtube",
        source_value=url,
        status="pending",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    background_tasks.add_task(process_job_pipeline, job.id)

    return RedirectResponse(url=f"/jobs/{job.id}/view", status_code=303)


@router.post("/jobs/{job_id}/view/retry")
def retry_job_from_page(
    job_id: int,
    force: str | None = Form(None),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    force_bool = force is not None
    job.status = "pending"
    job.error_message = None
    db.commit()

    background_tasks.add_task(process_job_pipeline, job.id, force_bool)
    return RedirectResponse(url=f"/jobs/{job.id}/view", status_code=303)


@router.post("/jobs/{job_id}/view/steps/{step_name}/retry")
def retry_job_step_from_page(
    job_id: int,
    step_name: str,
    force: str | None = Form(None),
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    force_bool = force is not None
    from app.services.pipeline import reset_pipeline_state_from_step, validate_step_name

    normalized_step = validate_step_name(step_name)
    reset_pipeline_state_from_step(db, job, normalized_step, reset_attempts=False)
    background_tasks.add_task(process_job_pipeline, job.id, force_bool, normalized_step)

    return RedirectResponse(url=f"/jobs/{job.id}/view", status_code=303)


@router.post("/jobs/{job_id}/view/steps/{step_name}/reset")
def reset_job_step_from_page(
    job_id: int,
    step_name: str,
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    from app.services.pipeline import reset_pipeline_state_from_step, validate_step_name

    normalized_step = validate_step_name(step_name)
    reset_pipeline_state_from_step(db, job, normalized_step, reset_attempts=True)

    return RedirectResponse(url=f"/jobs/{job.id}/view", status_code=303)


@router.post("/jobs/{job_id}/view/feedback/recalibrate")
def recalibrate_feedback_from_page(
    job_id: int,
    mode: str = Form("short"),
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job nÃ£o encontrado")

    normalized_mode = _normalize_mode(mode)
    learn_keywords_for_niche(db, niche=(job.detected_niche or "geral").lower().strip())

    return RedirectResponse(url=f"/jobs/{job.id}/view?mode={normalized_mode}", status_code=303)


@router.get("/jobs/{job_id}/view")
def job_detail(
    job_id: int,
    request: Request,
    mode: str = "short",
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    normalized_mode = _normalize_mode(mode)
    candidates = []
    feedback_profile = None
    if job.transcript_path and job.status == "done":
        feedback_profile = get_feedback_profile_for_niche(db, job.detected_niche or "geral", normalized_mode)
        ranked = _get_ranked_candidates(db, job, mode=normalized_mode)[:10]
        candidates = enrich_candidates_for_view(ranked, mode=normalized_mode)

    clips = (
        db.query(Clip)
        .filter(Clip.job_id == job_id)
        .order_by(Clip.created_at.desc())
        .all()
    )
    steps = get_job_steps(db, job.id)

    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            "job": job,
            "mode": normalized_mode,
            "candidates": candidates,
            "clips": enrich_clips_for_view(clips),
            "steps": enrich_steps_for_view(steps),
            "feedback_profile": enrich_feedback_profile_for_view(feedback_profile),
            "video_url": build_static_url(job.video_path),
            "audio_url": build_static_url(job.audio_path),
            "transcript_url": build_static_url(job.transcript_path),
            "build_static_url": build_static_url,
        },
    )


@router.post("/jobs/{job_id}/view/render-candidate")
def render_candidate_from_page(
    job_id: int,
    candidate_index: int = Form(...),
    mode: str = Form(...),
    burn_subtitles: bool = Form(False),
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")
    if not job.video_path or not job.transcript_path:
        raise HTTPException(status_code=400, detail="Job incompleto")

    normalized_mode = _normalize_mode(mode)
    ranked = _get_ranked_candidates(db, job, mode=normalized_mode)
    if candidate_index < 0 or candidate_index >= len(ranked):
        raise HTTPException(status_code=400, detail="candidate_index inválido")

    candidate = ranked[candidate_index]

    subtitles_path = None
    if burn_subtitles:
        subtitles_path = generate_ass_for_clip(
            transcript_path=job.transcript_path,
            job_id=job.id,
            clip_index=candidate_index,
            clip_start=candidate["start"],
            clip_end=candidate["end"],
            mode=normalized_mode,
        )

    output_path = render_clip(
        video_path=job.video_path,
        job_id=job.id,
        clip_index=candidate_index,
        start=candidate["start"],
        end=candidate["end"],
        mode=normalized_mode,
        burn_subtitles=burn_subtitles,
        subtitles_path=subtitles_path,
    )

    clip = Clip(
        job_id=job.id,
        source="candidate",
        mode=normalized_mode,
        start_time=candidate["start"],
        end_time=candidate["end"],
        duration=candidate["duration"],
        score=candidate.get("score"),
        reason=candidate.get("reason"),
        text=candidate.get("text"),
        subtitles_burned=burn_subtitles,
        output_path=output_path,
    )
    db.add(clip)
    db.commit()

    return RedirectResponse(url=f"/jobs/{job.id}/view?mode={normalized_mode}", status_code=303)


@router.post("/jobs/{job_id}/view/render-manual")
def render_manual_from_page(
    job_id: int,
    start: float = Form(...),
    end: float = Form(...),
    mode: str = Form(...),
    burn_subtitles: str | None = Form(None),
    db: Session = Depends(get_db),
):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")
    if not job.video_path or not job.transcript_path:
        raise HTTPException(status_code=400, detail="Job incompleto")

    normalized_mode = _normalize_mode(mode)
    if end <= start:
        raise HTTPException(status_code=400, detail="end deve ser maior que start")

    burn_subtitles_bool = burn_subtitles is not None
    duration = round(end - start, 2)

    subtitles_path = None
    if burn_subtitles_bool:
        subtitles_path = generate_ass_for_clip(
            transcript_path=job.transcript_path,
            job_id=job.id,
            clip_index=9999,
            clip_start=start,
            clip_end=end,
            mode=normalized_mode,
        )

    output_path = render_clip(
        video_path=job.video_path,
        job_id=job.id,
        clip_index=9999,
        start=start,
        end=end,
        mode=normalized_mode,
        burn_subtitles=burn_subtitles_bool,
        subtitles_path=subtitles_path,
    )

    clip = Clip(
        job_id=job.id,
        source="manual",
        mode=normalized_mode,
        start_time=start,
        end_time=end,
        duration=duration,
        score=None,
        reason="Render manual via interface web",
        text=None,
        subtitles_burned=burn_subtitles_bool,
        output_path=output_path,
    )
    db.add(clip)
    db.commit()

    return RedirectResponse(url=f"/jobs/{job.id}/view?mode={normalized_mode}", status_code=303)
