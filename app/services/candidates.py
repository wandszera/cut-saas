import json

from sqlalchemy.orm import Session

from app.models.candidate import Candidate
from app.models.job import Job
from app.core.config import settings
from app.services.llm_analysis import analyze_candidates_with_llm
from app.services.niche_learning import (
    get_feedback_profile_for_niche,
    get_hybrid_weights_for_niche,
    get_learned_keywords_for_niche,
)
from app.services.niche_registry import get_niche_profile
from app.services.segmentation import load_segments, build_candidate_windows
from app.services.scoring import score_candidates


def rerank_candidates_if_enabled(
    candidates: list[dict],
    mode: str,
    db: Session | None = None,
    *,
    niche: str = "geral",
) -> list[dict]:
    if not candidates or not settings.llm_rerank_enabled:
        return candidates

    top_n = max(1, min(settings.llm_top_n, len(candidates)))
    head = candidates[:top_n]
    tail = candidates[top_n:]
    hybrid_weights = (
        get_hybrid_weights_for_niche(db, niche, mode)
        if db is not None
        else {"heuristic_weight": 0.65, "llm_weight": 0.35}
    )

    try:
        reranked_head = analyze_candidates_with_llm(
            head,
            mode=mode,
            heuristic_weight=hybrid_weights["heuristic_weight"],
            llm_weight=hybrid_weights["llm_weight"],
        )
    except Exception:
        return candidates

    return reranked_head + tail

def regenerate_candidates_for_job(db: Session, job: Job, mode: str) -> list[Candidate]:
    (
        db.query(Candidate)
        .filter(Candidate.job_id == job.id, Candidate.mode == mode)
        .delete()
    )
    db.commit()

    raw_segments = load_segments(job.transcript_path)
    candidates = build_candidate_windows(raw_segments, mode=mode)

    niche = job.detected_niche or "geral"
    niche_profile = get_niche_profile(db, niche)
    learned_keywords = get_learned_keywords_for_niche(db, niche)
    feedback_profile = get_feedback_profile_for_niche(db, niche, mode)
    transcript_insights = json.loads(job.transcript_insights) if job.transcript_insights else None
    ranked = score_candidates(
        candidates,
        mode=mode,
        niche=niche,
        niche_profile=niche_profile,
        learned_keywords=learned_keywords,
        feedback_profile=feedback_profile,
        transcript_insights=transcript_insights,
    )
    ranked = rerank_candidates_if_enabled(ranked, mode=mode, db=db, niche=niche)

    created = []
    for item in ranked:
        candidate = Candidate(
            job_id=job.id,
            mode=mode,
            start_time=item["start"],
            end_time=item["end"],
            duration=item["duration"],
            heuristic_score=item.get("base_score", item.get("score")),
            score=item["score"],
            reason=item.get("reason"),
            opening_text=item.get("opening_text"),
            closing_text=item.get("closing_text"),
            full_text=item.get("text"),
            hook_score=item.get("hook_score"),
            clarity_score=item.get("clarity_score"),
            closure_score=item.get("closure_score"),
            emotion_score=item.get("emotion_score"),
            duration_fit_score=item.get("duration_fit_score"),
            transcript_context_score=item.get("transcript_context_score"),
            llm_score=item.get("llm_score"),
            llm_why=item.get("llm_why"),
            llm_title=item.get("llm_title"),
            llm_hook=item.get("llm_hook"),
            status="pending",
        )
        db.add(candidate)
        created.append(candidate)

    db.commit()

    for candidate in created:
        db.refresh(candidate)

    return created


def ensure_default_candidates_for_job(
    db: Session,
    job: Job,
    *,
    modes: tuple[str, ...] = ("short",),
    force: bool = False,
) -> dict[str, int]:
    summary: dict[str, int] = {}

    for mode in modes:
        existing_count = (
            db.query(Candidate)
            .filter(Candidate.job_id == job.id, Candidate.mode == mode)
            .count()
        )
        if existing_count and not force:
            summary[mode] = existing_count
            continue

        created = regenerate_candidates_for_job(db, job, mode)
        summary[mode] = len(created)

    return summary


def get_candidates_for_job(db: Session, job_id: int, mode: str) -> list[Candidate]:
    return (
        db.query(Candidate)
        .filter(Candidate.job_id == job_id, Candidate.mode == mode)
        .order_by(Candidate.score.desc(), Candidate.created_at.asc())
        .all()
    )
