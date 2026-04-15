from fastapi import APIRouter, Request, Form, Depends, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse
from app.db.database import get_db
from app.models.job import Job
from app.models.clip import Clip
from app.services.youtube import download_youtube_media
from app.services.audio import extract_audio_from_video
from app.services.transcription import transcribe_audio
from app.services.segmentation import load_segments, build_candidate_windows
from app.services.scoring import score_candidates
from app.services.clipping import render_clip
from app.services.subtitles import generate_ass_for_clip
from app.utils.media_urls import build_static_url

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory="app/templates")

def get_status_info(status: str) -> dict:
    status_map = {
        "pending": {"label": "Na fila", "progress": 5},
        "downloading": {"label": "Baixando vídeo", "progress": 20},
        "extracting_audio": {"label": "Extraindo áudio", "progress": 40},
        "transcribing": {"label": "Transcrevendo", "progress": 70},
        "analyzing": {"label": "Analisando", "progress": 85},
        "rendering": {"label": "Renderizando", "progress": 95},
        "done": {"label": "Concluído", "progress": 100},
        "failed": {"label": "Falhou", "progress": 100},
    }
    return status_map.get(status, {"label": status, "progress": 10})


def has_active_jobs(jobs: list[Job]) -> bool:
    active_statuses = {"pending", "downloading", "extracting_audio", "transcribing", "analyzing", "rendering"}
    return any(job.status in active_statuses for job in jobs)
def _get_ranked_candidates(job: Job, mode: str) -> list[dict]:
    raw_segments = load_segments(job.transcript_path)
    candidates = build_candidate_windows(raw_segments, mode=mode)
    ranked = score_candidates(candidates, mode=mode)
    return ranked


@router.get("/")
def home(request: Request, db: Session = Depends(get_db)):
    recent_jobs = (
        db.query(Job)
        .order_by(Job.created_at.desc())
        .limit(20)
        .all()
    )

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

    try:
        job.status = "downloading"
        db.commit()

        media = download_youtube_media(job.source_value, job.id)
        job.video_path = media.get("video_path")
        job.title = media.get("title")
        db.commit()

        job.status = "extracting_audio"
        db.commit()

        audio_path = extract_audio_from_video(job.video_path, job.id)
        job.audio_path = audio_path
        db.commit()

        job.status = "transcribing"
        db.commit()

        transcript_path = transcribe_audio(audio_path, job.id)
        job.transcript_path = transcript_path
        db.commit()

        job.status = "analyzing"
        db.commit()

        job.status = "done"
        db.commit()

    except Exception as e:
        job.status = "failed"
        job.error_message = str(e)
        db.commit()

    return RedirectResponse(url=f"/jobs/{job.id}/view", status_code=303)


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

    candidates = []
    if job.transcript_path and job.status == "done":
        if mode not in {"short", "long"}:
            mode = "short"
        candidates = _get_ranked_candidates(job, mode=mode)[:10]

    clips = (
        db.query(Clip)
        .filter(Clip.job_id == job_id)
        .order_by(Clip.created_at.desc())
        .all()
    )

    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            "job": job,
            "mode": mode,
            "candidates": candidates,
            "clips": clips,
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

    ranked = _get_ranked_candidates(job, mode=mode)
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
            mode=mode,
        )

    output_path = render_clip(
        video_path=job.video_path,
        job_id=job.id,
        clip_index=candidate_index,
        start=candidate["start"],
        end=candidate["end"],
        mode=mode,
        burn_subtitles=burn_subtitles,
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
        subtitles_burned=burn_subtitles,
        output_path=output_path,
    )
    db.add(clip)
    db.commit()

    return RedirectResponse(url=f"/jobs/{job.id}/view?mode={mode}", status_code=303)

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

    mode = mode.lower().strip()
    if mode not in {"short", "long"}:
        raise HTTPException(status_code=400, detail="mode deve ser 'short' ou 'long'")

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
            mode=mode,
        )

    output_path = render_clip(
        video_path=job.video_path,
        job_id=job.id,
        clip_index=9999,
        start=start,
        end=end,
        mode=mode,
        burn_subtitles=burn_subtitles_bool,
        subtitles_path=subtitles_path,
    )

    clip = Clip(
        job_id=job.id,
        source="manual",
        mode=mode,
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

    return RedirectResponse(url=f"/jobs/{job.id}/view?mode={mode}", status_code=303)

def format_seconds_to_mmss(seconds: float | int | None) -> str:
    if seconds is None:
        return "--:--"

    total = int(round(float(seconds)))
    minutes = total // 60
    secs = total % 60
    return f"{minutes:02}:{secs:02}"


def enrich_candidates_for_view(candidates: list[dict], mode: str) -> list[dict]:
    enriched = []

    for c in candidates:
        start = float(c.get("start", 0))
        end = float(c.get("end", 0))
        duration = float(c.get("duration", 0))
        score = float(c.get("score", 0))

        opening_text = c.get("opening_text") or c.get("text", "")[:180]
        closing_text = c.get("closing_text") or ""

        if score >= 10:
            score_label = "muito forte"
        elif score >= 7:
            score_label = "forte"
        elif score >= 4:
            score_label = "médio"
        else:
            score_label = "fraco"

        enriched.append({
            **c,
            "start_mmss": format_seconds_to_mmss(start),
            "end_mmss": format_seconds_to_mmss(end),
            "duration_mmss": format_seconds_to_mmss(duration),
            "time_range_label": f"{format_seconds_to_mmss(start)} → {format_seconds_to_mmss(end)}",
            "format_label": "9:16" if mode == "short" else "16:9",
            "opening_preview": opening_text[:220],
            "closing_preview": closing_text[:220],
            "score_label": score_label,
        })

    return enriched
