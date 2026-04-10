from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.job import Job
from app.schemas.job import JobCreateYouTube, JobResponse, RenderRequest
from app.services.youtube import download_youtube_media
from app.services.audio import extract_audio_from_video
from app.services.transcription import transcribe_audio
from app.services.clipping import render_clip
from app.services.subtitles import generate_ass_for_clip
from app.schemas.job import JobCreateYouTube, JobResponse, RenderRequest, AnalyzeRequest
from app.services.segmentation import load_segments, build_candidate_windows
from app.services.scoring import score_candidates

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

@router.get("/{job_id}", response_model=JobResponse)
def get_job(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job não encontrado")
    return job


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

    raw_segments = load_segments(job.transcript_path)
    candidates = build_candidate_windows(raw_segments, mode=mode)
    ranked = score_candidates(candidates, mode=mode)[:payload.top_n]

    return {
        "job_id": job.id,
        "title": job.title,
        "mode": mode,
        "total_candidates": len(ranked),
        "segments": ranked,
    }