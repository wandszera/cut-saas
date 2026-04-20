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
from app.services.analysis_calibration import build_analysis_calibration_profile
from app.services.segmentation import load_segments, build_candidate_windows, split_segments_into_time_chunks
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
    return regenerate_candidates_for_job_with_progress(db, job, mode)


def regenerate_candidates_for_job_with_progress(
    db: Session,
    job: Job,
    mode: str,
    *,
    progress_callback=None,
) -> list[Candidate]:
    (
        db.query(Candidate)
        .filter(Candidate.job_id == job.id, Candidate.mode == mode)
        .delete()
    )
    db.commit()

    if progress_callback:
        progress_callback("Carregando segmentos da transcricao", 58)
    raw_segments = load_segments(job.transcript_path)
    chunks = split_segments_into_time_chunks(raw_segments)
    total_chunks = len(chunks) or 1

    niche = job.detected_niche or "geral"
    niche_profile = get_niche_profile(db, niche)
    learned_keywords = get_learned_keywords_for_niche(db, niche)
    feedback_profile = get_feedback_profile_for_niche(db, niche, mode)
    transcript_insights = json.loads(job.transcript_insights) if job.transcript_insights else None
    calibration_profile = build_analysis_calibration_profile(db, niche=niche, mode=mode)
    created = []
    for chunk_index, chunk_segments in enumerate(chunks, start=1):
        if progress_callback:
            chunk_start = float(chunk_segments[0].get("start", 0.0) or 0.0)
            chunk_end = float(chunk_segments[-1].get("end", chunk_start) or chunk_start)
            chunk_percent = 58 + int(round((chunk_index - 1) / total_chunks * 30))
            progress_callback(
                f"Montando chunk {chunk_index}/{total_chunks} ({int(chunk_start)}s-{int(chunk_end)}s)",
                chunk_percent,
            )
        candidates = build_candidate_windows(chunk_segments, mode=mode)
        if progress_callback:
            progress_callback(
                f"Pontuando chunk {chunk_index}/{total_chunks} com {len(candidates)} candidato(s)",
                64 + int(round(chunk_index / total_chunks * 16)),
            )
        ranked = score_candidates(
            candidates,
            mode=mode,
            niche=niche,
            niche_profile=niche_profile,
            learned_keywords=learned_keywords,
            feedback_profile=feedback_profile,
            transcript_insights=transcript_insights,
            calibration_profile=calibration_profile,
        )
        if progress_callback:
            progress_callback(
                f"Aplicando rerank no chunk {chunk_index}/{total_chunks}",
                76 + int(round(chunk_index / total_chunks * 10)),
            )
        ranked = rerank_candidates_if_enabled(ranked, mode=mode, db=db, niche=niche)

        existing_windows = [
            {
                "start": row.start_time,
                "end": row.end_time,
                "duration": row.duration,
            }
            for row in db.query(Candidate).filter(Candidate.job_id == job.id, Candidate.mode == mode).all()
        ]
        chunk_new_count = 0
        total_ranked = len(ranked) or 1
        for item_index, item in enumerate(ranked, start=1):
            if _is_duplicate_candidate_window(item, existing_windows):
                continue

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
            existing_windows.append(
                {
                    "start": item["start"],
                    "end": item["end"],
                    "duration": item["duration"],
                }
            )
            chunk_new_count += 1
            if progress_callback and item_index in {1, total_ranked, max(1, total_ranked // 2)}:
                progress_callback(
                    f"Persistindo candidatos do chunk {chunk_index}/{total_chunks} ({chunk_new_count} novo(s))",
                    82 + int(round(chunk_index / total_chunks * 14)),
                )
        db.commit()
        if progress_callback:
            progress_callback(
                f"Chunk {chunk_index}/{total_chunks} concluido com {chunk_new_count} candidato(s) novo(s)",
                84 + int(round(chunk_index / total_chunks * 12)),
            )

    for candidate in created:
        db.refresh(candidate)

    return created


def _is_duplicate_candidate_window(
    candidate: dict,
    existing_windows: list[dict],
    *,
    time_tolerance: float = 8.0,
) -> bool:
    for existing in existing_windows:
        overlap_start = max(float(candidate["start"]), float(existing["start"]))
        overlap_end = min(float(candidate["end"]), float(existing["end"]))
        overlap = max(0.0, overlap_end - overlap_start)
        shorter = min(float(candidate["duration"]), float(existing["duration"])) or 1.0
        overlap_ratio = overlap / shorter
        if (
            abs(float(candidate["start"]) - float(existing["start"])) <= time_tolerance
            and abs(float(candidate["end"]) - float(existing["end"])) <= time_tolerance
        ) or overlap_ratio >= 0.9:
            return True
    return False


def ensure_default_candidates_for_job(
    db: Session,
    job: Job,
    *,
    modes: tuple[str, ...] = ("short",),
    force: bool = False,
    progress_callback=None,
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

        created = regenerate_candidates_for_job_with_progress(
            db,
            job,
            mode,
            progress_callback=progress_callback,
        )
        summary[mode] = len(created)

    return summary


def get_candidates_for_job(db: Session, job_id: int, mode: str) -> list[Candidate]:
    return (
        db.query(Candidate)
        .filter(Candidate.job_id == job_id, Candidate.mode == mode)
        .order_by(Candidate.score.desc(), Candidate.created_at.asc())
        .all()
    )
