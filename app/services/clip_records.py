from app.models.clip import Clip
from app.models.job import Job
from app.services.editorial import build_editorial_package


def build_clip_record(
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
) -> Clip:
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
    return Clip(
        job_id=job.id,
        source=source,
        mode=mode,
        start_time=start,
        end_time=end,
        duration=duration,
        score=score,
        reason=reason,
        text=text,
        headline=editorial["headline"],
        description=editorial["description"],
        hashtags=editorial["hashtags"],
        suggested_filename=editorial["suggested_filename"],
        render_preset=render_preset,
        publication_status="draft",
        subtitles_burned=subtitles_burned,
        output_path=output_path,
    )
