from fastapi import APIRouter, Depends, HTTPException, Form
from sqlalchemy.orm import Session
from fastapi import BackgroundTasks
from app.services.pipeline import process_job_pipeline
from app.db.database import get_db
from app.models.job import Job
from app.models.clip import Clip
from app.schemas.job import (
    JobCreateYouTube,
    JobResponse,
    RenderRequest,
    AnalyzeRequest,
    RenderCandidateRequest,
    ManualRenderRequest,
)
from app.models.candidate import Candidate
from app.services.candidates import regenerate_candidates_for_job, get_candidates_for_job
from app.services.youtube import download_youtube_media
from app.services.audio import extract_audio_from_video
from app.services.transcription import transcribe_audio
from app.services.segmentation import load_segments, build_candidate_windows
from app.services.scoring import score_candidates
from app.services.clipping import render_clip
from app.services.subtitles import generate_ass_for_clip
from app.utils.media_urls import build_static_url
def _get_ranked_candidates(job: Job, mode: str) -> list[dict]:
    raw_segments = load_segments(job.transcript_path)
    candidates = build_candidate_windows(raw_segments, mode=mode)
    ranked = score_candidates(candidates, mode=mode)
    return ranked

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.post("/youtube", response_model=JobResponse)
def create_youtube_job(payload: JobCreateYouTube, db: Session = Depends(get_db)):
    job = Job(
        source_type="youtube",
        source_value=str(payload.url),
        status="pending"
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    try:
        job.status = "processing"
        db.commit()

        media_data = download_youtube_media(str(payload.url), job.id)
        video_path = media_data["video_path"]

        job.title = media_data["title"]
        job.video_path = video_path

        audio_path = extract_audio_from_video(video_path, job.id)
        job.audio_path = audio_path

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
        raise HTTPException(status_code=500, detail=f"Erro ao processar job: {e}")
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

    background_tasks.add_task(process_job_pipeline, job.id, db)

    return RedirectResponse(url=f"/jobs/{job.id}/view", status_code=303)
@router.post("/{job_id}/render")
def render_top_clips(job_id: int, payload: RenderRequest, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    if not job.video_path:
        raise HTTPException(status_code=400, detail="Job não possui vídeo")

    if not job.transcript_path:
        raise HTTPException(status_code=400, detail="Job não possui transcrição")

    mode = payload.mode.lower().strip()
    if mode not in {"short", "long"}:
        raise HTTPException(status_code=400, detail="mode deve ser 'short' ou 'long'")

    raw_segments = load_segments(job.transcript_path)
    candidates = build_candidate_windows(raw_segments, mode=mode)
    ranked = score_candidates(candidates, mode=mode)

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

        rendered.append({
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
        })

    return {
        "job_id": job.id,
        "title": job.title,
        "mode": mode,
        "format": "9:16" if mode == "short" else "16:9",
        "rendered_clips_count": len(rendered),
        "burn_subtitles": payload.burn_subtitles,
        "clips": rendered,
    }

@router.get("/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

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
    }


@router.post("/{job_id}/analyze")
def analyze_job(job_id: int, payload: AnalyzeRequest, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    if not job.transcript_path:
        raise HTTPException(status_code=400, detail="Job ainda não possui transcrição")

    mode = payload.mode.lower().strip()
    if mode not in {"short", "long"}:
        raise HTTPException(status_code=400, detail="mode deve ser 'short' ou 'long'")

    saved_candidates = regenerate_candidates_for_job(db, job, mode=mode)

    return {
        "job_id": job.id,
        "title": job.title,
        "mode": mode,
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
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    mode = mode.lower().strip()
    if mode not in {"short", "long"}:
        raise HTTPException(status_code=400, detail="mode deve ser 'short' ou 'long'")

    candidates = get_candidates_for_job(db, job_id=job.id, mode=mode)

    return {
        "job_id": job.id,
        "title": job.title,
        "mode": mode,
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
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    candidate = (
        db.query(Candidate)
        .filter(Candidate.id == candidate_id, Candidate.job_id == job_id)
        .first()
    )
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidato não encontrado")

    if not job.video_path or not job.transcript_path:
        raise HTTPException(status_code=400, detail="Job incompleto")

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
@router.post("/{job_id}/analyze")
def analyze_job(job_id: int, payload: AnalyzeRequest, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    if not job.transcript_path:
        raise HTTPException(status_code=400, detail="Job ainda não possui transcrição")

    mode = payload.mode.lower().strip()
    if mode not in {"short", "long"}:
        raise HTTPException(status_code=400, detail="mode deve ser 'short' ou 'long'")

    ranked = _get_ranked_candidates(job, mode=mode)

    return {
        "job_id": job.id,
        "title": job.title,
        "mode": mode,
        "total_candidates": len(ranked),
        "segments": ranked[:payload.top_n],
    }
@router.post("/{job_id}/render-candidate")
def render_candidate(job_id: int, payload: RenderCandidateRequest, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    if not job.video_path:
        raise HTTPException(status_code=400, detail="Job não possui vídeo")

    if not job.transcript_path:
        raise HTTPException(status_code=400, detail="Job não possui transcrição")

    mode = payload.mode.lower().strip()
    if mode not in {"short", "long"}:
        raise HTTPException(status_code=400, detail="mode deve ser 'short' ou 'long'")

    ranked = _get_ranked_candidates(job, mode=mode)

    if payload.candidate_index >= len(ranked):
        raise HTTPException(
            status_code=400,
            detail=f"candidate_index inválido. Total disponível: {len(ranked)}"
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
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    mode = mode.lower().strip()
    if mode not in {"short", "long"}:
        raise HTTPException(status_code=400, detail="mode deve ser 'short' ou 'long'")

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
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    if not job.video_path or not job.transcript_path:
        raise HTTPException(status_code=400, detail="Job incompleto")

    mode = mode.lower().strip()
    if mode not in {"short", "long"}:
        raise HTTPException(status_code=400, detail="mode deve ser 'short' ou 'long'")

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

        rendered.append({
            "candidate_id": candidate.id,
            "clip_output_path": output_path,
            "start": candidate.start_time,
            "end": candidate.end_time,
            "duration": candidate.duration,
            "score": candidate.score,
        })

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
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

    if not job.video_path:
        raise HTTPException(status_code=400, detail="Job não possui vídeo")

    if not job.transcript_path:
        raise HTTPException(status_code=400, detail="Job não possui transcrição")

    mode = payload.mode.lower().strip()
    if mode not in {"short", "long"}:
        raise HTTPException(status_code=400, detail="mode deve ser 'short' ou 'long'")

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
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")

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