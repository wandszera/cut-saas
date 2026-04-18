from app.models.candidate import Candidate
from app.models.clip import Clip
from app.utils.media_urls import build_static_url


def serialize_candidate(candidate: Candidate) -> dict:
    return {
        "candidate_id": getattr(candidate, "id", None),
        "start": getattr(candidate, "start_time", None),
        "end": getattr(candidate, "end_time", None),
        "duration": getattr(candidate, "duration", None),
        "heuristic_score": getattr(candidate, "heuristic_score", None),
        "score": getattr(candidate, "score", None),
        "reason": getattr(candidate, "reason", None),
        "opening_text": getattr(candidate, "opening_text", None),
        "closing_text": getattr(candidate, "closing_text", None),
        "text": getattr(candidate, "full_text", None),
        "hook_score": getattr(candidate, "hook_score", None),
        "clarity_score": getattr(candidate, "clarity_score", None),
        "closure_score": getattr(candidate, "closure_score", None),
        "emotion_score": getattr(candidate, "emotion_score", None),
        "duration_fit_score": getattr(candidate, "duration_fit_score", None),
        "transcript_context_score": getattr(candidate, "transcript_context_score", None),
        "llm_score": getattr(candidate, "llm_score", None),
        "llm_why": getattr(candidate, "llm_why", None),
        "llm_title": getattr(candidate, "llm_title", None),
        "llm_hook": getattr(candidate, "llm_hook", None),
        "status": getattr(candidate, "status", None),
        "is_favorite": getattr(candidate, "is_favorite", False),
        "editorial_notes": getattr(candidate, "editorial_notes", None),
    }


def serialize_clip(clip: Clip) -> dict:
    return {
        "clip_id": clip.id,
        "id": clip.id,
        "job_id": clip.job_id,
        "source": clip.source,
        "mode": clip.mode,
        "format": "9:16" if clip.mode == "short" else "16:9",
        "start": clip.start_time,
        "end": clip.end_time,
        "start_time": clip.start_time,
        "end_time": clip.end_time,
        "duration": clip.duration,
        "score": clip.score,
        "reason": clip.reason,
        "text": clip.text,
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
