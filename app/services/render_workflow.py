from typing import Any

from sqlalchemy.orm import Session

from app.models.candidate import Candidate
from app.models.clip import Clip
from app.models.job import Job
from app.services.clip_records import build_clip_record
from app.services.clipping import render_clip
from app.services.subtitles import generate_ass_for_clip


def render_candidate_clip(
    *,
    db: Session,
    job: Job,
    candidate: Candidate,
    burn_subtitles: bool,
    render_preset: str,
    clip_index: int | None = None,
    mark_candidate_rendered: bool = True,
) -> tuple[Clip, str | None, str]:
    effective_clip_index = candidate.id if clip_index is None else clip_index
    subtitles_path = None
    if burn_subtitles:
        subtitles_path = generate_ass_for_clip(
            transcript_path=job.transcript_path,
            job_id=job.id,
            clip_index=effective_clip_index,
            clip_start=candidate.start_time,
            clip_end=candidate.end_time,
            mode=candidate.mode,
            render_preset=render_preset,
        )

    output_path = render_clip(
        video_path=job.video_path,
        job_id=job.id,
        clip_index=effective_clip_index,
        start=candidate.start_time,
        end=candidate.end_time,
        mode=candidate.mode,
        burn_subtitles=burn_subtitles,
        subtitles_path=subtitles_path,
        render_preset=render_preset,
    )

    clip = build_clip_record(
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
    db.add(clip)

    if mark_candidate_rendered:
        candidate.status = "rendered"

    return clip, subtitles_path, output_path


def render_ranked_candidate_clip(
    *,
    db: Session,
    job: Job,
    candidate: dict[str, Any],
    mode: str,
    burn_subtitles: bool,
    render_preset: str,
    clip_index: int,
) -> tuple[Clip, str | None, str]:
    subtitles_path = None
    if burn_subtitles:
        subtitles_path = generate_ass_for_clip(
            transcript_path=job.transcript_path,
            job_id=job.id,
            clip_index=clip_index,
            clip_start=candidate["start"],
            clip_end=candidate["end"],
            mode=mode,
            render_preset=render_preset,
        )

    output_path = render_clip(
        video_path=job.video_path,
        job_id=job.id,
        clip_index=clip_index,
        start=candidate["start"],
        end=candidate["end"],
        mode=mode,
        burn_subtitles=burn_subtitles,
        subtitles_path=subtitles_path,
        render_preset=render_preset,
    )

    clip = build_clip_record(
        job=job,
        source="candidate",
        mode=mode,
        start=candidate["start"],
        end=candidate["end"],
        duration=candidate["duration"],
        score=candidate.get("score"),
        reason=candidate.get("reason"),
        text=candidate.get("text"),
        subtitles_burned=burn_subtitles,
        output_path=output_path,
        render_preset=render_preset,
    )
    db.add(clip)
    return clip, subtitles_path, output_path


def render_manual_clip(
    *,
    db: Session,
    job: Job,
    start: float,
    end: float,
    mode: str,
    burn_subtitles: bool,
    render_preset: str,
    clip_index: int = 9999,
    reason: str = "Render manual",
) -> tuple[Clip, str | None, str]:
    duration = round(end - start, 2)
    subtitles_path = None
    if burn_subtitles:
        subtitles_path = generate_ass_for_clip(
            transcript_path=job.transcript_path,
            job_id=job.id,
            clip_index=clip_index,
            clip_start=start,
            clip_end=end,
            mode=mode,
            render_preset=render_preset,
        )

    output_path = render_clip(
        video_path=job.video_path,
        job_id=job.id,
        clip_index=clip_index,
        start=start,
        end=end,
        mode=mode,
        burn_subtitles=burn_subtitles,
        subtitles_path=subtitles_path,
        render_preset=render_preset,
    )

    clip = build_clip_record(
        job=job,
        source="manual",
        mode=mode,
        start=start,
        end=end,
        duration=duration,
        score=None,
        reason=reason,
        text=None,
        subtitles_burned=burn_subtitles,
        output_path=output_path,
        render_preset=render_preset,
    )
    db.add(clip)
    return clip, subtitles_path, output_path
