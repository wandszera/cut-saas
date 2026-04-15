from sqlalchemy.orm import Session

from app.models.candidate import Candidate
from app.models.job import Job
from app.services.segmentation import load_segments, build_candidate_windows
from app.services.scoring import score_candidates


def regenerate_candidates_for_job(db: Session, job: Job, mode: str) -> list[Candidate]:
    # apaga candidatos antigos desse modo
    (
        db.query(Candidate)
        .filter(Candidate.job_id == job.id, Candidate.mode == mode)
        .delete()
    )
    db.commit()

    raw_segments = load_segments(job.transcript_path)
    candidates = build_candidate_windows(raw_segments, mode=mode)
    ranked = score_candidates(candidates, mode=mode)

    created = []
    for item in ranked:
        candidate = Candidate(
            job_id=job.id,
            mode=mode,
            start_time=item["start"],
            end_time=item["end"],
            duration=item["duration"],
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
            status="pending",
        )
        db.add(candidate)
        created.append(candidate)

    db.commit()

    for candidate in created:
        db.refresh(candidate)

    return created


def get_candidates_for_job(db: Session, job_id: int, mode: str) -> list[Candidate]:
    return (
        db.query(Candidate)
        .filter(Candidate.job_id == job_id, Candidate.mode == mode)
        .order_by(Candidate.score.desc(), Candidate.created_at.asc())
        .all()
    )