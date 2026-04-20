from __future__ import annotations

from collections import Counter

from sqlalchemy.orm import Session

from app.models.candidate import Candidate
from app.models.job import Job


INFORMATIVE_OPENING_PATTERNS = (
    "hoje eu vou",
    "hoje eu quero",
    "nesse video",
    "nesse vídeo",
    "neste video",
    "neste vídeo",
    "eu quero falar",
    "eu vou falar",
    "vamos falar",
    "vou comentar",
    "vou te contar",
)

CONTEXTUAL_OPENING_PREFIXES = (
    "ele ",
    "ela ",
    "eles ",
    "elas ",
    "isso ",
    "esse ",
    "essa ",
    "aqui ",
    "então ",
    "entao ",
    "agora ",
    "daí ",
    "dai ",
)

POSITIVE_STATUSES = {"approved", "rendered"}
NEGATIVE_STATUSES = {"rejected"}


def _normalize(text: str | None) -> str:
    return " ".join((text or "").strip().lower().split())


def _percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * ratio)))
    return round(ordered[index], 2)


def _is_positive(candidate: Candidate) -> bool:
    return candidate.status in POSITIVE_STATUSES or bool(candidate.is_favorite)


def _is_negative(candidate: Candidate) -> bool:
    return candidate.status in NEGATIVE_STATUSES


def _opening_signature(text: str | None) -> str:
    normalized = _normalize(text)
    return " ".join(normalized.split()[:8])


def _is_informative_opening(text: str | None) -> bool:
    normalized = _normalize(text)
    return any(normalized.startswith(pattern) for pattern in INFORMATIVE_OPENING_PATTERNS)


def _is_contextual_opening(text: str | None) -> bool:
    normalized = _normalize(text)
    return any(normalized.startswith(pattern) for pattern in CONTEXTUAL_OPENING_PREFIXES)


def build_analysis_calibration_profile(
    db: Session,
    *,
    mode: str = "short",
    niche: str | None = None,
) -> dict:
    normalized_mode = (mode or "short").strip().lower()
    normalized_niche = (niche or "").strip().lower()

    query = (
        db.query(Candidate, Job)
        .join(Job, Job.id == Candidate.job_id)
        .filter(Candidate.mode == normalized_mode)
    )
    if normalized_niche:
        query = query.filter(Job.detected_niche == normalized_niche)

    rows = query.order_by(Candidate.created_at.desc()).all()
    candidates = [candidate for candidate, _job in rows]
    reviewed = [candidate for candidate in candidates if _is_positive(candidate) or _is_negative(candidate)]
    positive = [candidate for candidate in reviewed if _is_positive(candidate)]
    negative = [candidate for candidate in reviewed if _is_negative(candidate)]

    positive_durations = [float(candidate.duration or 0.0) for candidate in positive]
    negative_durations = [float(candidate.duration or 0.0) for candidate in negative]

    positive_signatures = [_opening_signature(candidate.opening_text) for candidate in positive if _opening_signature(candidate.opening_text)]
    negative_signatures = [_opening_signature(candidate.opening_text) for candidate in negative if _opening_signature(candidate.opening_text)]
    positive_duplicates = sum(count for count in Counter(positive_signatures).values() if count > 1)
    negative_duplicates = sum(count for count in Counter(negative_signatures).values() if count > 1)

    positive_informative = sum(1 for candidate in positive if _is_informative_opening(candidate.opening_text))
    negative_informative = sum(1 for candidate in negative if _is_informative_opening(candidate.opening_text))
    positive_contextual = sum(1 for candidate in positive if _is_contextual_opening(candidate.opening_text))
    negative_contextual = sum(1 for candidate in negative if _is_contextual_opening(candidate.opening_text))

    positive_count = len(positive)
    negative_count = len(negative)
    reviewed_count = len(reviewed)

    positive_duplicate_rate = round(positive_duplicates / positive_count, 2) if positive_count else 0.0
    negative_duplicate_rate = round(negative_duplicates / negative_count, 2) if negative_count else 0.0
    positive_informative_rate = round(positive_informative / positive_count, 2) if positive_count else 0.0
    negative_informative_rate = round(negative_informative / negative_count, 2) if negative_count else 0.0
    positive_contextual_rate = round(positive_contextual / positive_count, 2) if positive_count else 0.0
    negative_contextual_rate = round(negative_contextual / negative_count, 2) if negative_count else 0.0

    preferred_short_max_seconds = 120.0
    if normalized_mode == "short" and positive_durations:
        positive_p75 = _percentile(positive_durations, 0.75) or 90.0
        preferred_short_max_seconds = max(75.0, min(120.0, positive_p75 + 5.0))

    diversity_penalty_multiplier = 1.0
    if negative_duplicate_rate >= 0.25:
        diversity_penalty_multiplier += 0.35
    if negative_duplicate_rate > positive_duplicate_rate + 0.1:
        diversity_penalty_multiplier += 0.2

    informative_opening_multiplier = 1.0
    if negative_informative_rate >= 0.2:
        informative_opening_multiplier += 0.2
    if negative_informative_rate > positive_informative_rate + 0.12:
        informative_opening_multiplier += 0.25

    context_penalty_multiplier = 1.0
    if negative_contextual_rate >= 0.2:
        context_penalty_multiplier += 0.2
    if negative_contextual_rate > positive_contextual_rate + 0.12:
        context_penalty_multiplier += 0.25

    recommendations = []
    if normalized_mode == "short" and preferred_short_max_seconds < 115:
        recommendations.append("Historico favorece shorts mais enxutos; manter penalizacao mais cedo para duracoes longas.")
    if diversity_penalty_multiplier > 1.2:
        recommendations.append("Historico mostra redundancia entre aberturas parecidas; aplicar diversidade mais forte no reranking.")
    if informative_opening_multiplier > 1.2:
        recommendations.append("Aberturas muito explicativas estao performando pior; pesar mais contra setups informativos.")
    if context_penalty_multiplier > 1.2:
        recommendations.append("Trechos que dependem de contexto anterior estao sendo rejeitados com frequencia; endurecer filtro contextual.")

    return {
        "mode": normalized_mode,
        "niche": normalized_niche or None,
        "reviewed_count": reviewed_count,
        "positive_count": positive_count,
        "negative_count": negative_count,
        "calibration_active": reviewed_count >= 3,
        "preferred_short_max_seconds": round(preferred_short_max_seconds, 2),
        "diversity_penalty_multiplier": round(diversity_penalty_multiplier, 2),
        "informative_opening_multiplier": round(informative_opening_multiplier, 2),
        "context_penalty_multiplier": round(context_penalty_multiplier, 2),
        "duration_summary": {
            "positive_p50": _percentile(positive_durations, 0.5),
            "positive_p75": _percentile(positive_durations, 0.75),
            "positive_p90": _percentile(positive_durations, 0.9),
            "negative_p50": _percentile(negative_durations, 0.5),
            "negative_p75": _percentile(negative_durations, 0.75),
        },
        "opening_patterns": {
            "positive_duplicate_rate": positive_duplicate_rate,
            "negative_duplicate_rate": negative_duplicate_rate,
            "positive_informative_rate": positive_informative_rate,
            "negative_informative_rate": negative_informative_rate,
            "positive_contextual_rate": positive_contextual_rate,
            "negative_contextual_rate": negative_contextual_rate,
        },
        "recommendations": recommendations,
    }
