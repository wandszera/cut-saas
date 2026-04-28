import re
from collections import Counter, defaultdict

from sqlalchemy.orm import Session

from app.models.candidate import Candidate
from app.models.job import Job
from app.models.niche_keyword import NicheKeyword


STOPWORDS = {
    "a", "o", "e", "de", "do", "da", "dos", "das", "em", "um", "uma",
    "para", "por", "com", "sem", "que", "não", "nao", "no", "na", "os",
    "as", "é", "ser", "foi", "era", "vai", "vou", "tem", "tá", "ta",
    "né", "ne", "isso", "essa", "esse", "assim", "então", "entao",
    "porque", "porquê", "por que", "como", "qual", "quais", "quando",
    "onde", "ele", "ela", "eles", "elas", "você", "voce", "vocês",
    "também", "tambem", "muito", "mais", "menos", "já", "ja", "eu",
    "tu", "me", "te", "se", "ao", "à", "às", "ou", "mas", "só", "so",
    "gente", "cara", "mano", "tipo"
}


def _tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    words = re.findall(r"\b[a-zA-ZÀ-ÿ0-9_-]{4,}\b", text)
    return [
        w for w in words
        if w not in STOPWORDS
        and not w.isdigit()
    ]


def learn_keywords_for_niche(
    db: Session,
    niche: str,
    min_candidate_score: float = 8.0,
    min_occurrences: int = 3,
    min_distinct_jobs: int = 2,
    workspace_id: int | None = None,
):
    if niche == "geral":
        return []

    query = db.query(Candidate).join(Job, Candidate.job_id == Job.id).filter(
        Candidate.score >= min_candidate_score
    )
    if workspace_id is not None:
        query = query.filter(Job.workspace_id == workspace_id)
    candidates = query.all()

    niche_candidates = [c for c in candidates if c.reason and c.full_text and niche]

    keyword_counts = Counter()
    keyword_jobs = defaultdict(set)

    for candidate in niche_candidates:
        tokens = set(_tokenize(candidate.full_text))
        for token in tokens:
            keyword_counts[token] += 1
            keyword_jobs[token].add(candidate.job_id)

    learned = []

    for keyword, occurrences in keyword_counts.items():
        distinct_jobs = len(keyword_jobs[keyword])

        if occurrences < min_occurrences:
            continue

        if distinct_jobs < min_distinct_jobs:
            continue

        score = round(occurrences * 1.0 + distinct_jobs * 1.5, 2)

        existing = (
            db.query(NicheKeyword)
            .filter(
                NicheKeyword.niche == niche,
                NicheKeyword.keyword == keyword,
                NicheKeyword.workspace_id == workspace_id,
            )
            .first()
        )

        if existing:
            existing.score = score
            existing.occurrences = occurrences
            existing.distinct_jobs = distinct_jobs
            existing.status = "active"
            learned.append(existing)
        else:
            nk = NicheKeyword(
                niche=niche,
                workspace_id=workspace_id,
                keyword=keyword,
                score=score,
                occurrences=occurrences,
                distinct_jobs=distinct_jobs,
                source="learned",
                status="active",
            )
            db.add(nk)
            learned.append(nk)

    db.commit()
    return learned


def get_learned_keywords_for_niche(
    db: Session,
    niche: str,
    workspace_id: int | None = None,
) -> list[str]:
    query = db.query(NicheKeyword).filter(
        NicheKeyword.niche == niche,
        NicheKeyword.status == "active",
    )
    if workspace_id is not None:
        query = query.filter(
            (NicheKeyword.workspace_id == workspace_id) | (NicheKeyword.workspace_id.is_(None))
        )
    else:
        query = query.filter(NicheKeyword.workspace_id.is_(None))
    rows = query.order_by(NicheKeyword.score.desc()).all()
    return [row.keyword for row in rows]


def _avg(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _collect_candidate_metrics(candidate: Candidate) -> dict[str, float]:
    return {
        "hook_score": float(candidate.hook_score or 0.0),
        "clarity_score": float(candidate.clarity_score or 0.0),
        "closure_score": float(candidate.closure_score or 0.0),
        "emotion_score": float(candidate.emotion_score or 0.0),
        "duration_fit_score": float(candidate.duration_fit_score or 0.0),
        "duration": float(candidate.duration or 0.0),
    }


def _is_divergent_candidate(candidate: Candidate, threshold: float = 1.2) -> bool:
    if candidate.llm_score is None or candidate.heuristic_score is None:
        return False
    return abs(float(candidate.heuristic_score) - float(candidate.llm_score)) >= threshold


def _build_hybrid_weight_profile(positive_candidates: list[Candidate], negative_candidates: list[Candidate]) -> dict:
    heuristic_evidence = 0
    llm_evidence = 0
    reviewed = 0
    approved = 0
    rejected = 0

    for candidate in positive_candidates:
        if not _is_divergent_candidate(candidate):
            continue
        reviewed += 1
        approved += 1
        if float(candidate.llm_score or 0.0) > float(candidate.heuristic_score or 0.0):
            llm_evidence += 1
        else:
            heuristic_evidence += 1

    for candidate in negative_candidates:
        if not _is_divergent_candidate(candidate):
            continue
        reviewed += 1
        rejected += 1
        if float(candidate.llm_score or 0.0) > float(candidate.heuristic_score or 0.0):
            heuristic_evidence += 1
        else:
            llm_evidence += 1

    if reviewed == 0:
        return {
            "reviewed_count": 0,
            "approved_count": 0,
            "rejected_count": 0,
            "preferred_source": "balanced",
            "heuristic_weight": 0.65,
            "llm_weight": 0.35,
        }

    evidence_gap = heuristic_evidence - llm_evidence
    heuristic_weight = min(0.8, max(0.5, 0.65 + (evidence_gap * 0.05)))
    llm_weight = round(1.0 - heuristic_weight, 2)
    heuristic_weight = round(heuristic_weight, 2)

    if evidence_gap >= 2:
        preferred_source = "heuristic"
    elif evidence_gap <= -2:
        preferred_source = "llm"
    else:
        preferred_source = "balanced"

    return {
        "reviewed_count": reviewed,
        "approved_count": approved,
        "rejected_count": rejected,
        "preferred_source": preferred_source,
        "heuristic_weight": heuristic_weight,
        "llm_weight": llm_weight,
    }


def get_feedback_profile_for_niche(
    db: Session,
    niche: str,
    mode: str,
    *,
    min_samples: int = 2,
    workspace_id: int | None = None,
) -> dict:
    niche = (niche or "geral").lower().strip()
    mode = (mode or "short").lower().strip()

    query = (
        db.query(Candidate, Job)
        .join(Job, Candidate.job_id == Job.id)
        .filter(
            Candidate.mode == mode,
            Candidate.status.in_(("approved", "rejected", "rendered")),
        )
    )
    if workspace_id is not None:
        query = query.filter(Job.workspace_id == workspace_id)
    rows = query.all()

    niche_rows = [
        (candidate, job)
        for candidate, job in rows
        if (job.detected_niche or "geral").lower().strip() == niche
    ]

    positive_statuses = {"approved", "rendered"}
    positive_candidates = [candidate for candidate, _job in niche_rows if candidate.status in positive_statuses]
    negative_candidates = [candidate for candidate, _job in niche_rows if candidate.status == "rejected"]

    metrics = [
        "hook_score",
        "clarity_score",
        "closure_score",
        "emotion_score",
        "duration_fit_score",
        "duration",
    ]

    profile = {
        "niche": niche,
        "mode": mode,
        "positive_count": len(positive_candidates),
        "negative_count": len(negative_candidates),
        "sample_count": len(niche_rows),
        "min_samples_reached": len(positive_candidates) >= min_samples,
        "positive_means": {},
        "negative_means": {},
        "successful_keywords": [],
        "hybrid_weight_profile": _build_hybrid_weight_profile(positive_candidates, negative_candidates),
    }

    if not profile["min_samples_reached"]:
        return profile

    for metric in metrics:
        profile["positive_means"][metric] = _avg(
            [_collect_candidate_metrics(candidate)[metric] for candidate in positive_candidates]
        )
        profile["negative_means"][metric] = _avg(
            [_collect_candidate_metrics(candidate)[metric] for candidate in negative_candidates]
        )

    token_counter = Counter()
    for candidate in positive_candidates:
        token_counter.update(set(_tokenize(candidate.full_text or "")))

    profile["successful_keywords"] = [
        keyword
        for keyword, _count in token_counter.most_common(12)
    ]
    return profile


def get_hybrid_weights_for_niche(db: Session, niche: str, mode: str) -> dict[str, float]:
    profile = get_feedback_profile_for_niche(db, niche, mode)
    hybrid_profile = profile.get("hybrid_weight_profile", {})
    return {
        "heuristic_weight": float(hybrid_profile.get("heuristic_weight", 0.65) or 0.65),
        "llm_weight": float(hybrid_profile.get("llm_weight", 0.35) or 0.35),
    }
