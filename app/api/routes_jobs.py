import json
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.candidate import Candidate
from app.models.clip import Clip
from app.models.job import Job
from app.models.niche_keyword import NicheKeyword
from app.schemas.job import (
    AnalyzeRequest,
    CandidateNotesRequest,
    JobCreateLocalVideo,
    JobCreateYouTube,
    JobResponse,
    ManualRenderRequest,
    RenderCandidateRequest,
    RenderRequest,
)
from app.services.candidates import get_candidates_for_job, regenerate_candidates_for_job
from app.services.audio import extract_audio_from_video
from app.services.clipping import render_clip
from app.services.editorial import build_editorial_package
from app.services.exports import build_job_export_bundle, list_job_export_bundles
from app.services.niche_learning import (
    get_feedback_profile_for_niche,
    get_learned_keywords_for_niche,
    learn_keywords_for_niche,
)
from app.services.niche_registry import get_niche_profile
from app.services.render_presets import list_render_presets
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


def _get_candidate_or_404(db: Session, candidate_id: int) -> Candidate:
    candidate = db.query(Candidate).filter(Candidate.id == candidate_id).first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidato não encontrado")
    return candidate


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
    niche_profile = get_niche_profile(db, niche)
    learned_keywords = get_learned_keywords_for_niche(db, niche)
    feedback_profile = get_feedback_profile_for_niche(db, niche, mode)
    transcript_insights = json.loads(job.transcript_insights) if job.transcript_insights else None
    return score_candidates(
        candidates,
        mode=mode,
        niche=niche,
        niche_profile=niche_profile,
        learned_keywords=learned_keywords,
        feedback_profile=feedback_profile,
        transcript_insights=transcript_insights,
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
        "hybrid_weight_profile": profile.get("hybrid_weight_profile", {}),
    }


def _summarize_numeric_distribution(values: list[float]) -> dict:
    if not values:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "avg": None,
            "p50": None,
            "p90": None,
        }

    ordered = sorted(float(value) for value in values)

    def _percentile(ratio: float) -> float:
        index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * ratio)))
        return round(ordered[index], 2)

    return {
        "count": len(ordered),
        "min": round(ordered[0], 2),
        "max": round(ordered[-1], 2),
        "avg": round(sum(ordered) / len(ordered), 2),
        "p50": _percentile(0.5),
        "p90": _percentile(0.9),
    }


def _build_score_buckets(values: list[float]) -> list[dict]:
    buckets = [
        {"label": ">= 9", "min": 9.0, "max": None, "count": 0},
        {"label": "8 - 8.99", "min": 8.0, "max": 8.99, "count": 0},
        {"label": "7 - 7.99", "min": 7.0, "max": 7.99, "count": 0},
        {"label": "< 7", "min": None, "max": 6.99, "count": 0},
    ]

    for value in values:
        score = float(value)
        if score >= 9.0:
            buckets[0]["count"] += 1
        elif score >= 8.0:
            buckets[1]["count"] += 1
        elif score >= 7.0:
            buckets[2]["count"] += 1
        else:
            buckets[3]["count"] += 1

    return buckets


def _build_duration_buckets(values: list[float]) -> list[dict]:
    buckets = [
        {"label": "< 30s", "min_seconds": None, "max_seconds": 29.99, "count": 0},
        {"label": "30s - 59s", "min_seconds": 30.0, "max_seconds": 59.99, "count": 0},
        {"label": "60s - 89s", "min_seconds": 60.0, "max_seconds": 89.99, "count": 0},
        {"label": ">= 90s", "min_seconds": 90.0, "max_seconds": None, "count": 0},
    ]

    for value in values:
        duration = float(value)
        if duration < 30.0:
            buckets[0]["count"] += 1
        elif duration < 60.0:
            buckets[1]["count"] += 1
        elif duration < 90.0:
            buckets[2]["count"] += 1
        else:
            buckets[3]["count"] += 1

    return buckets


def _build_ranking_insights_payload(
    *,
    job: Job,
    mode: str,
    feedback_profile: dict | None,
    candidates: list[Candidate],
) -> dict:
    feedback_profile = feedback_profile or {}
    hybrid_weight_profile = feedback_profile.get("hybrid_weight_profile", {}) or {}

    candidate_payloads = [
        _build_api_candidate_payload(candidate, feedback_profile)
        for candidate in candidates
    ]
    divergences = [
        item for item in candidate_payloads
        if item.get("divergence_score") is not None
    ]
    strong_divergences = [
        item for item in divergences
        if float(item["divergence_score"]) >= 2.2
    ]
    moderate_or_stronger_divergences = [
        item for item in divergences
        if float(item["divergence_score"]) >= 1.2
    ]
    llm_favored = [
        item for item in moderate_or_stronger_divergences
        if (item.get("llm_score") or 0.0) > (item.get("heuristic_score") or 0.0)
    ]
    heuristic_favored = [
        item for item in moderate_or_stronger_divergences
        if (item.get("heuristic_score") or 0.0) > (item.get("llm_score") or 0.0)
    ]

    final_scores = [float(candidate.score or 0.0) for candidate in candidates]
    heuristic_scores = [
        float(candidate.heuristic_score)
        for candidate in candidates
        if candidate.heuristic_score is not None
    ]
    llm_scores = [
        float(candidate.llm_score)
        for candidate in candidates
        if candidate.llm_score is not None
    ]
    durations = [float(candidate.duration or 0.0) for candidate in candidates]

    status_counts: dict[str, int] = {}
    for candidate in candidates:
        status_counts[candidate.status] = status_counts.get(candidate.status, 0) + 1

    top_divergent_candidates = sorted(
        moderate_or_stronger_divergences,
        key=lambda item: float(item["divergence_score"]),
        reverse=True,
    )[:5]

    return {
        "job_id": job.id,
        "title": job.title,
        "niche": job.detected_niche or "geral",
        "mode": mode,
        "weights": {
            "preferred_source": hybrid_weight_profile.get("preferred_source", "balanced"),
            "heuristic_weight": round(float(hybrid_weight_profile.get("heuristic_weight", 0.65) or 0.65), 2),
            "llm_weight": round(float(hybrid_weight_profile.get("llm_weight", 0.35) or 0.35), 2),
            "reviewed_count": int(hybrid_weight_profile.get("reviewed_count", 0) or 0),
            "approved_count": int(hybrid_weight_profile.get("approved_count", 0) or 0),
            "rejected_count": int(hybrid_weight_profile.get("rejected_count", 0) or 0),
        },
        "candidate_summary": {
            "total_candidates": len(candidates),
            "llm_scored_count": len(llm_scores),
            "divergent_count": len(moderate_or_stronger_divergences),
            "strong_divergence_count": len(strong_divergences),
            "favorite_count": sum(1 for candidate in candidates if candidate.is_favorite),
            "status_counts": status_counts,
        },
        "divergence_summary": {
            "compared_candidates": len(divergences),
            "moderate_or_higher_count": len(moderate_or_stronger_divergences),
            "strong_count": len(strong_divergences),
            "llm_favored_count": len(llm_favored),
            "heuristic_favored_count": len(heuristic_favored),
            "divergence_score_distribution": _summarize_numeric_distribution(
                [float(item["divergence_score"]) for item in divergences]
            ),
            "top_divergent_candidates": [
                {
                    "candidate_id": item["candidate_id"],
                    "start": item["start"],
                    "end": item["end"],
                    "score": item["score"],
                    "heuristic_score": item["heuristic_score"],
                    "llm_score": item["llm_score"],
                    "divergence_score": item["divergence_score"],
                    "divergence_label": item["divergence_label"],
                    "divergence_summary": item["divergence_summary"],
                    "status": item["status"],
                }
                for item in top_divergent_candidates
            ],
        },
        "distribution": {
            "final_score": {
                **_summarize_numeric_distribution(final_scores),
                "buckets": _build_score_buckets(final_scores),
            },
            "heuristic_score": _summarize_numeric_distribution(heuristic_scores),
            "llm_score": _summarize_numeric_distribution(llm_scores),
            "duration_seconds": {
                **_summarize_numeric_distribution(durations),
                "buckets": _build_duration_buckets(durations),
            },
        },
    }


def _build_api_candidate_payload(candidate, feedback_profile: dict | None = None) -> dict:
    heuristic_score = float(getattr(candidate, "heuristic_score", 0.0) or 0.0)
    llm_score_raw = getattr(candidate, "llm_score", None)
    llm_score = round(float(llm_score_raw), 2) if llm_score_raw is not None else None
    divergence_score = (
        round(abs(heuristic_score - llm_score), 2)
        if llm_score is not None
        else None
    )

    divergence_label = None
    divergence_summary = None
    if divergence_score is not None:
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

    hybrid_weight_profile = (feedback_profile or {}).get("hybrid_weight_profile", {}) or {}
    preferred_source = hybrid_weight_profile.get("preferred_source", "balanced")
    heuristic_weight = round(float(hybrid_weight_profile.get("heuristic_weight", 0.65) or 0.65), 2)
    llm_weight = round(float(hybrid_weight_profile.get("llm_weight", 0.35) or 0.35), 2)

    adaptive_blend_explanation = None
    if divergence_score is not None and divergence_score >= 1.2:
        if preferred_source == "heuristic":
            adaptive_blend_explanation = (
                f"Este corte subiu com mais apoio da heurística porque, neste nicho, "
                f"divergências recentes estão favorecendo o heurístico ({heuristic_weight} vs {llm_weight})."
            )
        elif preferred_source == "llm":
            adaptive_blend_explanation = (
                f"Este corte recebeu mais peso da LLM porque, neste nicho, "
                f"divergências recentes estão favorecendo a revisão da LLM ({llm_weight} vs {heuristic_weight})."
            )
        else:
            adaptive_blend_explanation = (
                f"Este corte ficou equilibrado porque o nicho ainda mantém pesos híbridos estáveis "
                f"({heuristic_weight} heurístico / {llm_weight} LLM)."
            )

    return {
        "candidate_id": candidate.id,
        "start": candidate.start_time,
        "end": candidate.end_time,
        "duration": candidate.duration,
        "heuristic_score": getattr(candidate, "heuristic_score", None),
        "score": candidate.score,
        "reason": candidate.reason,
        "opening_text": candidate.opening_text,
        "closing_text": candidate.closing_text,
        "text": candidate.full_text,
        "hook_score": candidate.hook_score,
        "clarity_score": candidate.clarity_score,
        "closure_score": candidate.closure_score,
        "emotion_score": candidate.emotion_score,
        "duration_fit_score": candidate.duration_fit_score,
        "transcript_context_score": getattr(candidate, "transcript_context_score", None),
        "llm_score": getattr(candidate, "llm_score", None),
        "llm_why": getattr(candidate, "llm_why", None),
        "llm_title": getattr(candidate, "llm_title", None),
        "llm_hook": getattr(candidate, "llm_hook", None),
        "status": candidate.status,
        "is_favorite": getattr(candidate, "is_favorite", False),
        "editorial_notes": getattr(candidate, "editorial_notes", None),
        "divergence_score": divergence_score,
        "divergence_label": divergence_label,
        "divergence_summary": divergence_summary,
        "adaptive_blend_explanation": adaptive_blend_explanation,
    }


def _build_clip_payload(
    *,
    job: Job,
    source: str,
    mode: str,
    start: float,
    end: float,
    duration: float,
    score: float | None,
    reason: str | None,
    text: str | None,
    subtitles_burned: bool,
    output_path: str,
    render_preset: str,
) -> dict:
    editorial = build_editorial_package(
        job_title=job.title,
        niche=job.detected_niche,
        mode=mode,
        clip_id=None,
        start=start,
        end=end,
        text=text,
        reason=reason,
        render_preset=render_preset,
    )
    return {
        "job_id": job.id,
        "source": source,
        "mode": mode,
        "start_time": start,
        "end_time": end,
        "duration": duration,
        "score": score,
        "reason": reason,
        "text": text,
        "headline": editorial["headline"],
        "description": editorial["description"],
        "hashtags": editorial["hashtags"],
        "suggested_filename": editorial["suggested_filename"],
        "render_preset": render_preset,
        "publication_status": "draft",
        "subtitles_burned": subtitles_burned,
        "output_path": output_path,
    }


@router.get("/debug/node")
def debug_node():
    return detect_node()


@router.get("/render-presets")
def get_render_presets():
    return {
        "default": "clean",
        "presets": list_render_presets(),
    }


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


@router.post("/local", response_model=JobResponse)
def create_local_video_job(payload: JobCreateLocalVideo, db: Session = Depends(get_db)):
    video_file = Path(payload.video_path).expanduser()
    if not video_file.exists() or not video_file.is_file():
        raise HTTPException(status_code=400, detail="video_path nao encontrado")

    resolved_title = (payload.title or video_file.stem).strip() or video_file.stem
    job = Job(
        source_type="local",
        source_value=str(video_file),
        status="pending",
        title=resolved_title,
        video_path=str(video_file),
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    try:
        job.status = "extracting_audio"
        db.commit()

        audio_path = extract_audio_from_video(job.video_path, job.id)
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
        raise HTTPException(status_code=500, detail=f"Erro ao processar job local: {e}") from e


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
                render_preset=payload.render_preset,
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
            render_preset=payload.render_preset,
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
                "render_preset": payload.render_preset,
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
        "render_preset": payload.render_preset,
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


@router.get("/{job_id}/ranking-insights")
def get_job_ranking_insights(job_id: int, mode: str = "short", db: Session = Depends(get_db)):
    job = _get_job_or_404(db, job_id)
    normalized_mode = _normalize_mode(mode)
    niche = job.detected_niche or "geral"
    feedback_profile = get_feedback_profile_for_niche(db, niche, normalized_mode)
    candidates = get_candidates_for_job(db, job_id=job.id, mode=normalized_mode)

    return _build_ranking_insights_payload(
        job=job,
        mode=normalized_mode,
        feedback_profile=feedback_profile,
        candidates=candidates,
    )


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
        "segments": [_build_api_candidate_payload(c, feedback_profile) for c in saved_candidates[:payload.top_n]],
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
        "candidates": [_build_api_candidate_payload(c, feedback_profile) for c in candidates],
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
            render_preset="clean",
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
        render_preset="clean",
    )

    clip = Clip(
        **_build_clip_payload(
            job=job,
            source="candidate",
            mode=candidate.mode,
            start=candidate.start_time,
            end=candidate.end_time,
            duration=candidate.duration,
            score=candidate.score,
            reason=candidate.reason,
            text=candidate.full_text,
            subtitles_burned=burn_subtitles,
            output_path=output_path,
            render_preset="clean",
        )
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
        "render_preset": "clean",
        "headline": clip.headline,
        "description": clip.description,
        "hashtags": clip.hashtags,
        "suggested_filename": clip.suggested_filename,
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
            render_preset=payload.render_preset,
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
        render_preset=payload.render_preset,
    )

    clip = Clip(
        **_build_clip_payload(
            job=job,
            source="candidate",
            mode=mode,
            start=candidate["start"],
            end=candidate["end"],
            duration=candidate["duration"],
            score=candidate.get("score"),
            reason=candidate.get("reason"),
            text=candidate.get("text"),
            subtitles_burned=payload.burn_subtitles,
            output_path=output_path,
            render_preset=payload.render_preset,
        )
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
        "render_preset": payload.render_preset,
        "headline": clip.headline,
        "description": clip.description,
        "hashtags": clip.hashtags,
        "suggested_filename": clip.suggested_filename,
        "subtitles_path": subtitles_path,
        "subtitles_url": build_static_url(subtitles_path),
        "output_path": output_path,
        "output_url": build_static_url(output_path),
    }


@router.post("/candidates/{candidate_id}/approve")
def approve_candidate(candidate_id: int, db: Session = Depends(get_db)):
    candidate = _get_candidate_or_404(db, candidate_id)

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
    candidate = _get_candidate_or_404(db, candidate_id)

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
    candidate = _get_candidate_or_404(db, candidate_id)

    candidate.status = "pending"
    db.commit()
    db.refresh(candidate)

    return {
        "message": "Status do candidato resetado",
        "candidate_id": candidate.id,
        "job_id": candidate.job_id,
        "status": candidate.status,
    }


@router.post("/candidates/{candidate_id}/favorite")
def toggle_candidate_favorite(candidate_id: int, db: Session = Depends(get_db)):
    candidate = _get_candidate_or_404(db, candidate_id)
    candidate.is_favorite = not bool(candidate.is_favorite)
    db.commit()
    db.refresh(candidate)

    return {
        "message": "Favorito atualizado com sucesso",
        "candidate_id": candidate.id,
        "job_id": candidate.job_id,
        "is_favorite": candidate.is_favorite,
    }


@router.post("/candidates/{candidate_id}/notes")
def update_candidate_notes(
    candidate_id: int,
    payload: CandidateNotesRequest,
    db: Session = Depends(get_db),
):
    candidate = _get_candidate_or_404(db, candidate_id)
    candidate.editorial_notes = payload.editorial_notes.strip() or None
    db.commit()
    db.refresh(candidate)

    return {
        "message": "Notas editoriais atualizadas com sucesso",
        "candidate_id": candidate.id,
        "job_id": candidate.job_id,
        "editorial_notes": candidate.editorial_notes,
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
        "candidates": [_build_api_candidate_payload(c) for c in candidates],
    }


@router.post("/{job_id}/render-approved")
def render_approved_candidates(
    job_id: int,
    mode: str = "short",
    burn_subtitles: bool = False,
    render_preset: str = "clean",
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
                render_preset=render_preset,
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
            render_preset=render_preset,
        )

        clip = Clip(
            **_build_clip_payload(
                job=job,
                source="candidate",
                mode=candidate.mode,
                start=candidate.start_time,
                end=candidate.end_time,
                duration=candidate.duration,
                score=candidate.score,
                reason=candidate.reason,
                text=candidate.full_text,
                subtitles_burned=burn_subtitles,
                output_path=output_path,
                render_preset=render_preset,
            )
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
                "render_preset": render_preset,
            }
        )

    db.commit()

    return {
        "job_id": job.id,
        "mode": mode,
        "burn_subtitles": burn_subtitles,
        "render_preset": render_preset,
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
            render_preset=payload.render_preset,
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
        render_preset=payload.render_preset,
    )

    clip = Clip(
        **_build_clip_payload(
            job=job,
            source="manual",
            mode=mode,
            start=payload.start,
            end=payload.end,
            duration=duration,
            score=None,
            reason="Render manual",
            text=None,
            subtitles_burned=payload.burn_subtitles,
            output_path=output_path,
            render_preset=payload.render_preset,
        )
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
        "render_preset": payload.render_preset,
        "headline": clip.headline,
        "description": clip.description,
        "hashtags": clip.hashtags,
        "suggested_filename": clip.suggested_filename,
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
                "headline": clip.headline,
                "description": clip.description,
                "hashtags": clip.hashtags,
                "suggested_filename": clip.suggested_filename,
                "render_preset": clip.render_preset,
                "publication_status": clip.publication_status,
                "subtitles_burned": clip.subtitles_burned,
                "output_path": clip.output_path,
                "output_url": build_static_url(clip.output_path),
                "created_at": clip.created_at,
            }
            for clip in clips
        ],
    }


@router.get("/{job_id}/export")
def export_job_bundle(job_id: int, db: Session = Depends(get_db)):
    job = _get_job_or_404(db, job_id)
    clips = (
        db.query(Clip)
        .filter(Clip.job_id == job_id)
        .order_by(Clip.created_at.desc())
        .all()
    )
    if not clips:
        raise HTTPException(status_code=400, detail="Nenhum clip renderizado para exportar")

    zip_path = build_job_export_bundle(job, clips)
    return FileResponse(
        path=zip_path,
        media_type="application/zip",
        filename=Path(zip_path).name,
    )


@router.post("/clips/{clip_id}/publication")
def update_clip_publication_status(
    clip_id: int,
    status: str,
    db: Session = Depends(get_db),
):
    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clip não encontrado")

    normalized_status = (status or "").strip().lower()
    allowed_statuses = {"draft", "ready", "published", "discarded"}
    if normalized_status not in allowed_statuses:
        raise HTTPException(status_code=400, detail="Status de publicação inválido")

    clip.publication_status = normalized_status
    db.commit()
    db.refresh(clip)

    return {
        "clip_id": clip.id,
        "job_id": clip.job_id,
        "publication_status": clip.publication_status,
    }


@router.get("/{job_id}/exports")
def list_job_exports(job_id: int, db: Session = Depends(get_db)):
    job = _get_job_or_404(db, job_id)
    exports = list_job_export_bundles(job.id)
    return {
        "job_id": job.id,
        "title": job.title,
        "total_exports": len(exports),
        "exports": [
            {
                "name": row["name"],
                "size_bytes": row["size_bytes"],
                "modified_at": row["modified_at"],
                "download_url": f"/jobs/{job.id}/export/files/{row['name']}",
            }
            for row in exports
        ],
    }


@router.get("/{job_id}/export/files/{filename}")
def download_existing_export(job_id: int, filename: str, db: Session = Depends(get_db)):
    _get_job_or_404(db, job_id)
    exports = {row["name"]: row for row in list_job_export_bundles(job_id)}
    target = exports.get(filename)
    if not target:
        raise HTTPException(status_code=404, detail="Pacote de exportação não encontrado")

    return FileResponse(
        path=target["path"],
        media_type="application/zip",
        filename=filename,
    )
