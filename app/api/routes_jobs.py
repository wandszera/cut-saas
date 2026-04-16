import json

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.candidate import Candidate
from app.models.clip import Clip
from app.models.job import Job
from app.models.niche_keyword import NicheKeyword
from app.schemas.job import (
    AnalyzeRequest,
    JobCreateYouTube,
    JobResponse,
    ManualRenderRequest,
    RenderCandidateRequest,
    RenderRequest,
)
from app.services.candidates import get_candidates_for_job, regenerate_candidates_for_job
from app.services.audio import extract_audio_from_video
from app.services.clipping import render_clip
from app.services.niche_learning import (
    get_feedback_profile_for_niche,
    get_learned_keywords_for_niche,
    learn_keywords_for_niche,
)
from app.services.pipeline import (
    MAX_STEP_ATTEMPTS,
    get_exhausted_steps,
    get_job_steps,
    process_job_pipeline,
    reset_pipeline_state_from_step,
    validate_step_name,
)
from app.services.scoring import score_candidates
from app.services.segmentation import build_candidate_windows, load_segments
from app.services.subtitles import generate_ass_for_clip
from app.services.transcription import transcribe_audio
from app.services.youtube import download_youtube_media
from app.utils.media_urls import build_static_url
from app.utils.runtime_env import detect_node


router = APIRouter(prefix="/jobs", tags=["jobs"])


def _get_job_or_404(db: Session, job_id: int) -> Job:
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")
    return job


def _normalize_mode(mode: str) -> str:
    normalized = mode.lower().strip()
    if normalized not in {"short", "long"}:
        raise HTTPException(status_code=400, detail="mode deve ser 'short' ou 'long'")
    return normalized


def _normalize_pipeline_step(step_name: str) -> str:
    try:
        return validate_step_name(step_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _ensure_job_ready_for_render(job: Job) -> None:
    if not job.video_path:
        raise HTTPException(status_code=400, detail="Job não possui vídeo")
    if not job.transcript_path:
        raise HTTPException(status_code=400, detail="Job não possui transcrição")


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


def _parse_step_details(raw_details: str | None) -> dict:
    if not raw_details:
        return {}
    try:
        payload = json.loads(raw_details)
    except json.JSONDecodeError:
        return {"raw_details": raw_details}
    return payload if isinstance(payload, dict) else {"value": payload}


def _serialize_step_response(step) -> dict:
    details_payload = _parse_step_details(step.details)
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

    return {
        "id": step.id,
        "step_name": step.step_name,
        "status": step.status,
        "attempts": step.attempts,
        "max_attempts": MAX_STEP_ATTEMPTS,
        "can_retry": step.status in {"failed", "pending"},
        "can_force_retry": step.status == "exhausted",
        "is_exhausted": step.status == "exhausted",
        "error_message": step.error_message,
        "details": step.details,
        "details_payload": details_payload,
        "summary_items": summary_items,
        "duration_seconds": duration_seconds,
        "duration_label": duration_label,
        "started_at": step.started_at,
        "completed_at": step.completed_at,
    }


def _serialize_feedback_profile(profile: dict | None) -> dict:
    profile = profile or {}
    return {
        "niche": profile.get("niche"),
        "mode": profile.get("mode"),
        "positive_count": profile.get("positive_count", 0),
        "negative_count": profile.get("negative_count", 0),
        "sample_count": profile.get("sample_count", 0),
        "min_samples_reached": profile.get("min_samples_reached", False),
        "successful_keywords": profile.get("successful_keywords", []),
        "positive_means": profile.get("positive_means", {}),
        "negative_means": profile.get("negative_means", {}),
    }


@router.get("/debug/node")
def debug_node():
    return detect_node()


@router.post("/youtube", response_model=JobResponse)
def create_youtube_job(payload: JobCreateYouTube, db: Session = Depends(get_db)):
    job = Job(
        source_type="youtube",
        source_value=str(payload.url),
        status="pending",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    try:
        job.status = "downloading"
        db.commit()

        media_data = download_youtube_media(str(payload.url), job.id)
        video_path = media_data["video_path"]

        job.title = media_data["title"]
        job.video_path = video_path

        job.status = "extracting_audio"
        db.commit()

        audio_path = extract_audio_from_video(video_path, job.id)
        job.audio_path = audio_path

        job.status = "transcribing"
        db.commit()

        transcript_path = transcribe_audio(audio_path, job.id)
        job.transcript_path = transcript_path

        job.status = "done"
        db.commit()
        db.refresh(job)

        return job
    except Exception as e:
        job.status = "failed"
        job.error_message = str(e)
        db.commit()
        raise HTTPException(status_code=500, detail=f"Erro ao processar job: {e}") from e


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


@router.post("/{job_id}/retry")
def retry_job(
    job_id: int,
    background_tasks: BackgroundTasks,
    force: bool = False,
    db: Session = Depends(get_db),
):
    job = _get_job_or_404(db, job_id)

    if job.status not in {"failed", "pending"}:
        raise HTTPException(
            status_code=400,
            detail="Apenas jobs com status 'failed' ou 'pending' podem ser reprocessados",
        )

    exhausted_steps = get_exhausted_steps(db, job.id)
    if exhausted_steps and not force:
        raise HTTPException(
            status_code=400,
            detail=(
                "Uma ou mais etapas excederam o limite de tentativas. "
                "Use force=true para tentar novamente."
            ),
        )

    job.status = "pending"
    job.error_message = None
    db.commit()

    background_tasks.add_task(process_job_pipeline, job.id, force)

    return {
        "message": "Reprocessamento agendado com sucesso",
        "job_id": job.id,
        "status": job.status,
        "force": force,
    }


@router.post("/{job_id}/steps/{step_name}/retry")
def retry_job_step(
    job_id: int,
    step_name: str,
    background_tasks: BackgroundTasks,
    force: bool = False,
    db: Session = Depends(get_db),
):
    job = _get_job_or_404(db, job_id)
    normalized_step = _normalize_pipeline_step(step_name)

    if job.status not in {"failed", "pending"}:
        raise HTTPException(
            status_code=400,
            detail="Apenas jobs com status 'failed' ou 'pending' podem reprocessar etapas",
        )

    steps = get_job_steps(db, job.id)
    step_map = {step.step_name: step for step in steps}
    target_step = step_map.get(normalized_step)
    if target_step and target_step.status == "exhausted" and not force:
        raise HTTPException(
            status_code=400,
            detail=(
                f"A etapa '{normalized_step}' excedeu o limite de tentativas. "
                "Use force=true para tentar novamente."
            ),
        )

    reset_pipeline_state_from_step(db, job, normalized_step, reset_attempts=False)
    background_tasks.add_task(process_job_pipeline, job.id, force, normalized_step)

    return {
        "message": "Reprocessamento da etapa agendado com sucesso",
        "job_id": job.id,
        "step_name": normalized_step,
        "status": job.status,
        "force": force,
    }


@router.post("/{job_id}/steps/{step_name}/reset")
def reset_job_step(
    job_id: int,
    step_name: str,
    db: Session = Depends(get_db),
):
    job = _get_job_or_404(db, job_id)
    normalized_step = _normalize_pipeline_step(step_name)

    reset_pipeline_state_from_step(db, job, normalized_step, reset_attempts=True)

    return {
        "message": "Etapa resetada com sucesso",
        "job_id": job.id,
        "step_name": normalized_step,
        "status": job.status,
        "reset_attempts": True,
    }


@router.post("/{job_id}/render")
def render_top_clips(job_id: int, payload: RenderRequest, db: Session = Depends(get_db)):
    job = _get_job_or_404(db, job_id)
    _ensure_job_ready_for_render(job)

    mode = _normalize_mode(payload.mode)
    ranked = _get_ranked_candidates(db, job, mode=mode)

    top_clips = ranked[:payload.top_n]
    rendered = []

    for index, clip in enumerate(top_clips):
        subtitles_path = None
        if payload.burn_subtitles:
            subtitles_path = generate_ass_for_clip(
                transcript_path=job.transcript_path,
                job_id=job.id,
                clip_index=index,
                clip_start=clip["start"],
                clip_end=clip["end"],
                mode=mode,
            )

        output_path = render_clip(
            video_path=job.video_path,
            job_id=job.id,
            clip_index=index,
            start=clip["start"],
            end=clip["end"],
            mode=mode,
            burn_subtitles=payload.burn_subtitles,
            subtitles_path=subtitles_path,
        )

        rendered.append(
            {
                "clip_number": index + 1,
                "start": clip["start"],
                "end": clip["end"],
                "duration": clip["duration"],
                "score": clip["score"],
                "reason": clip["reason"],
                "text": clip["text"],
                "mode": mode,
                "format": "9:16" if mode == "short" else "16:9",
                "subtitles_burned": payload.burn_subtitles,
                "subtitles_path": subtitles_path,
                "output_path": output_path,
            }
        )

    return {
        "job_id": job.id,
        "title": job.title,
        "mode": mode,
        "format": "9:16" if mode == "short" else "16:9",
        "rendered_clips_count": len(rendered),
        "burn_subtitles": payload.burn_subtitles,
        "clips": rendered,
    }


@router.post("/niches/{niche}/learn-keywords")
def learn_keywords_endpoint(niche: str, db: Session = Depends(get_db)):
    niche = niche.lower().strip()
    learned = learn_keywords_for_niche(db, niche=niche)

    return {
        "niche": niche,
        "learned_count": len(learned),
        "keywords": [
            {
                "id": row.id,
                "keyword": row.keyword,
                "score": row.score,
                "occurrences": row.occurrences,
                "distinct_jobs": row.distinct_jobs,
                "source": row.source,
                "status": row.status,
            }
            for row in learned
        ],
    }


@router.get("/niches/{niche}/keywords")
def list_keywords_by_niche(niche: str, db: Session = Depends(get_db)):
    niche = niche.lower().strip()
    rows = (
        db.query(NicheKeyword)
        .filter(NicheKeyword.niche == niche)
        .order_by(NicheKeyword.score.desc(), NicheKeyword.keyword.asc())
        .all()
    )

    return {
        "niche": niche,
        "total_keywords": len(rows),
        "keywords": [
            {
                "id": row.id,
                "keyword": row.keyword,
                "score": row.score,
                "occurrences": row.occurrences,
                "distinct_jobs": row.distinct_jobs,
                "source": row.source,
                "status": row.status,
            }
            for row in rows
        ],
    }


@router.get("/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db)):
    job = _get_job_or_404(db, job_id)
    steps = get_job_steps(db, job.id)
    exhausted_steps = get_exhausted_steps(db, job.id)

    return {
        "id": job.id,
        "source_type": job.source_type,
        "source_value": job.source_value,
        "status": job.status,
        "title": job.title,
        "video_path": job.video_path,
        "video_url": build_static_url(job.video_path),
        "audio_path": job.audio_path,
        "audio_url": build_static_url(job.audio_path),
        "transcript_path": job.transcript_path,
        "transcript_url": build_static_url(job.transcript_path),
        "result_path": job.result_path,
        "error_message": job.error_message,
        "created_at": job.created_at,
        "can_retry": job.status in {"failed", "pending"} and not exhausted_steps,
        "can_force_retry": job.status in {"failed", "pending"},
        "has_exhausted_steps": bool(exhausted_steps),
        "max_step_attempts": MAX_STEP_ATTEMPTS,
        "steps": [_serialize_step_response(step) for step in steps],
    }


@router.get("/{job_id}/feedback-profile")
def get_job_feedback_profile(job_id: int, mode: str = "short", db: Session = Depends(get_db)):
    job = _get_job_or_404(db, job_id)
    normalized_mode = _normalize_mode(mode)
    niche = job.detected_niche or "geral"
    feedback_profile = get_feedback_profile_for_niche(db, niche, normalized_mode)

    return {
        "job_id": job.id,
        "title": job.title,
        "niche": niche,
        "mode": normalized_mode,
        "feedback_profile": _serialize_feedback_profile(feedback_profile),
    }


@router.post("/{job_id}/feedback-profile/recalibrate")
def recalibrate_job_feedback_profile(
    job_id: int,
    mode: str = "short",
    db: Session = Depends(get_db),
):
    job = _get_job_or_404(db, job_id)
    normalized_mode = _normalize_mode(mode)
    niche = (job.detected_niche or "geral").lower().strip()
    learned = learn_keywords_for_niche(db, niche=niche)
    feedback_profile = get_feedback_profile_for_niche(db, niche, normalized_mode)

    return {
        "message": "Aprendizado recalibrado com sucesso",
        "job_id": job.id,
        "title": job.title,
        "niche": niche,
        "mode": normalized_mode,
        "learned_keywords_count": len(learned),
        "feedback_profile": _serialize_feedback_profile(feedback_profile),
    }


@router.post("/{job_id}/analyze")
def analyze_job(job_id: int, payload: AnalyzeRequest, db: Session = Depends(get_db)):
    job = _get_job_or_404(db, job_id)
    if not job.transcript_path:
        raise HTTPException(status_code=400, detail="Job ainda não possui transcrição")

    mode = _normalize_mode(payload.mode)
    feedback_profile = get_feedback_profile_for_niche(db, job.detected_niche or "geral", mode)
    saved_candidates = regenerate_candidates_for_job(db, job, mode=mode)

    return {
        "job_id": job.id,
        "title": job.title,
        "mode": mode,
        "feedback_profile": _serialize_feedback_profile(feedback_profile),
        "total_candidates": len(saved_candidates),
        "segments": [
            {
                "candidate_id": c.id,
                "start": c.start_time,
                "end": c.end_time,
                "duration": c.duration,
                "score": c.score,
                "reason": c.reason,
                "opening_text": c.opening_text,
                "closing_text": c.closing_text,
                "text": c.full_text,
                "hook_score": c.hook_score,
                "clarity_score": c.clarity_score,
                "closure_score": c.closure_score,
                "emotion_score": c.emotion_score,
                "duration_fit_score": c.duration_fit_score,
                "status": c.status,
            }
            for c in saved_candidates[:payload.top_n]
        ],
    }


@router.get("/{job_id}/candidates")
def list_candidates(job_id: int, mode: str = "short", db: Session = Depends(get_db)):
    job = _get_job_or_404(db, job_id)
    mode = _normalize_mode(mode)
    feedback_profile = get_feedback_profile_for_niche(db, job.detected_niche or "geral", mode)
    candidates = get_candidates_for_job(db, job_id=job.id, mode=mode)

    return {
        "job_id": job.id,
        "title": job.title,
        "mode": mode,
        "feedback_profile": _serialize_feedback_profile(feedback_profile),
        "total_candidates": len(candidates),
        "candidates": [
            {
                "candidate_id": c.id,
                "start": c.start_time,
                "end": c.end_time,
                "duration": c.duration,
                "score": c.score,
                "reason": c.reason,
                "opening_text": c.opening_text,
                "closing_text": c.closing_text,
                "text": c.full_text,
                "hook_score": c.hook_score,
                "clarity_score": c.clarity_score,
                "closure_score": c.closure_score,
                "emotion_score": c.emotion_score,
                "duration_fit_score": c.duration_fit_score,
                "status": c.status,
            }
            for c in candidates
        ],
    }


@router.post("/{job_id}/render-candidate-id/{candidate_id}")
def render_candidate_by_id(
    job_id: int,
    candidate_id: int,
    burn_subtitles: bool = False,
    db: Session = Depends(get_db),
):
    job = _get_job_or_404(db, job_id)
    candidate = (
        db.query(Candidate)
        .filter(Candidate.id == candidate_id, Candidate.job_id == job_id)
        .first()
    )
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidato não encontrado")

    _ensure_job_ready_for_render(job)

    subtitles_path = None
    if burn_subtitles:
        subtitles_path = generate_ass_for_clip(
            transcript_path=job.transcript_path,
            job_id=job.id,
            clip_index=candidate.id,
            clip_start=candidate.start_time,
            clip_end=candidate.end_time,
            mode=candidate.mode,
        )

    output_path = render_clip(
        video_path=job.video_path,
        job_id=job.id,
        clip_index=candidate.id,
        start=candidate.start_time,
        end=candidate.end_time,
        mode=candidate.mode,
        burn_subtitles=burn_subtitles,
        subtitles_path=subtitles_path,
    )

    clip = Clip(
        job_id=job.id,
        source="candidate",
        mode=candidate.mode,
        start_time=candidate.start_time,
        end_time=candidate.end_time,
        duration=candidate.duration,
        score=candidate.score,
        reason=candidate.reason,
        text=candidate.full_text,
        subtitles_burned=burn_subtitles,
        output_path=output_path,
    )
    db.add(clip)

    candidate.status = "rendered"

    db.commit()
    db.refresh(clip)

    return {
        "clip_id": clip.id,
        "candidate_id": candidate.id,
        "job_id": job.id,
        "mode": candidate.mode,
        "start": candidate.start_time,
        "end": candidate.end_time,
        "duration": candidate.duration,
        "score": candidate.score,
        "reason": candidate.reason,
        "subtitles_burned": burn_subtitles,
        "output_path": output_path,
    }


@router.post("/{job_id}/render-candidate")
def render_candidate(job_id: int, payload: RenderCandidateRequest, db: Session = Depends(get_db)):
    job = _get_job_or_404(db, job_id)
    _ensure_job_ready_for_render(job)

    mode = _normalize_mode(payload.mode)
    ranked = _get_ranked_candidates(db, job, mode=mode)

    if payload.candidate_index >= len(ranked):
        raise HTTPException(
            status_code=400,
            detail=f"candidate_index inválido. Total disponível: {len(ranked)}",
        )

    candidate = ranked[payload.candidate_index]

    subtitles_path = None
    if payload.burn_subtitles:
        subtitles_path = generate_ass_for_clip(
            transcript_path=job.transcript_path,
            job_id=job.id,
            clip_index=payload.candidate_index,
            clip_start=candidate["start"],
            clip_end=candidate["end"],
            mode=mode,
        )

    output_path = render_clip(
        video_path=job.video_path,
        job_id=job.id,
        clip_index=payload.candidate_index,
        start=candidate["start"],
        end=candidate["end"],
        mode=mode,
        burn_subtitles=payload.burn_subtitles,
        subtitles_path=subtitles_path,
    )

    clip = Clip(
        job_id=job.id,
        source="candidate",
        mode=mode,
        start_time=candidate["start"],
        end_time=candidate["end"],
        duration=candidate["duration"],
        score=candidate.get("score"),
        reason=candidate.get("reason"),
        text=candidate.get("text"),
        subtitles_burned=payload.burn_subtitles,
        output_path=output_path,
    )
    db.add(clip)
    db.commit()
    db.refresh(clip)

    return {
        "clip_id": clip.id,
        "job_id": job.id,
        "source": "candidate",
        "candidate_index": payload.candidate_index,
        "mode": mode,
        "format": "9:16" if mode == "short" else "16:9",
        "start": candidate["start"],
        "end": candidate["end"],
        "duration": candidate["duration"],
        "score": candidate.get("score"),
        "reason": candidate.get("reason"),
        "subtitles_burned": payload.burn_subtitles,
        "subtitles_path": subtitles_path,
        "subtitles_url": build_static_url(subtitles_path),
        "output_path": output_path,
        "output_url": build_static_url(output_path),
    }


@router.post("/candidates/{candidate_id}/approve")
def approve_candidate(candidate_id: int, db: Session = Depends(get_db)):
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidato não encontrado")

    candidate.status = "approved"
    db.commit()
    db.refresh(candidate)

    return {
        "message": "Candidato aprovado com sucesso",
        "candidate_id": candidate.id,
        "job_id": candidate.job_id,
        "status": candidate.status,
    }


@router.post("/candidates/{candidate_id}/reject")
def reject_candidate(candidate_id: int, db: Session = Depends(get_db)):
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidato não encontrado")

    candidate.status = "rejected"
    db.commit()
    db.refresh(candidate)

    return {
        "message": "Candidato rejeitado com sucesso",
        "candidate_id": candidate.id,
        "job_id": candidate.job_id,
        "status": candidate.status,
    }


@router.post("/candidates/{candidate_id}/reset")
def reset_candidate_status(candidate_id: int, db: Session = Depends(get_db)):
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidato não encontrado")

    candidate.status = "pending"
    db.commit()
    db.refresh(candidate)

    return {
        "message": "Status do candidato resetado",
        "candidate_id": candidate.id,
        "job_id": candidate.job_id,
        "status": candidate.status,
    }


@router.get("/{job_id}/approved-candidates")
def list_approved_candidates(job_id: int, mode: str = "short", db: Session = Depends(get_db)):
    job = _get_job_or_404(db, job_id)
    mode = _normalize_mode(mode)

    candidates = (
        db.query(Candidate)
        .filter(
            Candidate.job_id == job.id,
            Candidate.mode == mode,
            Candidate.status == "approved",
        )
        .order_by(Candidate.score.desc(), Candidate.created_at.asc())
        .all()
    )

    return {
        "job_id": job.id,
        "title": job.title,
        "mode": mode,
        "total_approved_candidates": len(candidates),
        "candidates": [
            {
                "candidate_id": c.id,
                "start": c.start_time,
                "end": c.end_time,
                "duration": c.duration,
                "score": c.score,
                "reason": c.reason,
                "opening_text": c.opening_text,
                "closing_text": c.closing_text,
                "text": c.full_text,
                "hook_score": c.hook_score,
                "clarity_score": c.clarity_score,
                "closure_score": c.closure_score,
                "emotion_score": c.emotion_score,
                "duration_fit_score": c.duration_fit_score,
                "status": c.status,
            }
            for c in candidates
        ],
    }


@router.post("/{job_id}/render-approved")
def render_approved_candidates(
    job_id: int,
    mode: str = "short",
    burn_subtitles: bool = False,
    db: Session = Depends(get_db),
):
    job = _get_job_or_404(db, job_id)
    _ensure_job_ready_for_render(job)
    mode = _normalize_mode(mode)

    approved_candidates = (
        db.query(Candidate)
        .filter(
            Candidate.job_id == job.id,
            Candidate.mode == mode,
            Candidate.status == "approved",
        )
        .order_by(Candidate.score.desc(), Candidate.created_at.asc())
        .all()
    )

    rendered = []

    for candidate in approved_candidates:
        subtitles_path = None
        if burn_subtitles:
            subtitles_path = generate_ass_for_clip(
                transcript_path=job.transcript_path,
                job_id=job.id,
                clip_index=candidate.id,
                clip_start=candidate.start_time,
                clip_end=candidate.end_time,
                mode=candidate.mode,
            )

        output_path = render_clip(
            video_path=job.video_path,
            job_id=job.id,
            clip_index=candidate.id,
            start=candidate.start_time,
            end=candidate.end_time,
            mode=candidate.mode,
            burn_subtitles=burn_subtitles,
            subtitles_path=subtitles_path,
        )

        clip = Clip(
            job_id=job.id,
            source="candidate",
            mode=candidate.mode,
            start_time=candidate.start_time,
            end_time=candidate.end_time,
            duration=candidate.duration,
            score=candidate.score,
            reason=candidate.reason,
            text=candidate.full_text,
            subtitles_burned=burn_subtitles,
            output_path=output_path,
        )
        db.add(clip)

        candidate.status = "rendered"
        rendered.append(
            {
                "candidate_id": candidate.id,
                "clip_output_path": output_path,
                "start": candidate.start_time,
                "end": candidate.end_time,
                "duration": candidate.duration,
                "score": candidate.score,
            }
        )

    db.commit()

    return {
        "job_id": job.id,
        "mode": mode,
        "burn_subtitles": burn_subtitles,
        "rendered_count": len(rendered),
        "clips": rendered,
    }


@router.post("/{job_id}/render-manual")
def render_manual_clip(job_id: int, payload: ManualRenderRequest, db: Session = Depends(get_db)):
    job = _get_job_or_404(db, job_id)
    _ensure_job_ready_for_render(job)

    mode = _normalize_mode(payload.mode)
    if payload.end <= payload.start:
        raise HTTPException(status_code=400, detail="end deve ser maior que start")

    duration = round(payload.end - payload.start, 2)

    subtitles_path = None
    if payload.burn_subtitles:
        subtitles_path = generate_ass_for_clip(
            transcript_path=job.transcript_path,
            job_id=job.id,
            clip_index=9999,
            clip_start=payload.start,
            clip_end=payload.end,
            mode=mode,
        )

    output_path = render_clip(
        video_path=job.video_path,
        job_id=job.id,
        clip_index=9999,
        start=payload.start,
        end=payload.end,
        mode=mode,
        burn_subtitles=payload.burn_subtitles,
        subtitles_path=subtitles_path,
    )

    clip = Clip(
        job_id=job.id,
        source="manual",
        mode=mode,
        start_time=payload.start,
        end_time=payload.end,
        duration=duration,
        score=None,
        reason="Render manual",
        text=None,
        subtitles_burned=payload.burn_subtitles,
        output_path=output_path,
    )
    db.add(clip)
    db.commit()
    db.refresh(clip)

    return {
        "clip_id": clip.id,
        "job_id": job.id,
        "source": "manual",
        "mode": mode,
        "format": "9:16" if mode == "short" else "16:9",
        "start": payload.start,
        "end": payload.end,
        "duration": duration,
        "subtitles_burned": payload.burn_subtitles,
        "subtitles_path": subtitles_path,
        "subtitles_url": build_static_url(subtitles_path),
        "output_path": output_path,
        "output_url": build_static_url(output_path),
    }


@router.get("/{job_id}/clips")
def list_rendered_clips(job_id: int, db: Session = Depends(get_db)):
    job = _get_job_or_404(db, job_id)

    clips = (
        db.query(Clip)
        .filter(Clip.job_id == job_id)
        .order_by(Clip.created_at.desc())
        .all()
    )

    return {
        "job_id": job.id,
        "title": job.title,
        "total_clips": len(clips),
        "clips": [
            {
                "clip_id": clip.id,
                "source": clip.source,
                "mode": clip.mode,
                "format": "9:16" if clip.mode == "short" else "16:9",
                "start": clip.start_time,
                "end": clip.end_time,
                "duration": clip.duration,
                "score": clip.score,
                "reason": clip.reason,
                "subtitles_burned": clip.subtitles_burned,
                "output_path": clip.output_path,
                "output_url": build_static_url(clip.output_path),
                "created_at": clip.created_at,
            }
            for clip in clips
        ],
    }
