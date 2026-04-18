import re
from collections import Counter

from sqlalchemy.orm import Session

from app.services.niche_profiles import NICHE_PROFILES
from app.services.niche_registry import get_active_niche_profiles


def _normalize(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _count_keywords(text: str, keywords: list[str]) -> int:
    normalized = _normalize(text)
    return sum(normalized.count(keyword.lower()) for keyword in keywords)


def detect_niche(title: str | None, transcript_text: str | None, db: Session | None = None) -> dict:
    combined = f"{title or ''}\n{transcript_text or ''}"
    combined = _normalize(combined)

    niche_scores = {}
    profiles = get_active_niche_profiles(db) if db is not None else NICHE_PROFILES

    for niche, profile in profiles.items():
        keywords = profile.get("keywords", [])
        score = _count_keywords(combined, keywords)
        niche_scores[niche] = score

    ranked = Counter(niche_scores).most_common()
    top_niche, top_score = ranked[0]

    # fallback
    if top_score <= 0:
        return {
            "niche": "geral",
            "confidence": "baixa",
            "scores": niche_scores,
        }

    second_score = ranked[1][1] if len(ranked) > 1 else 0
    diff = top_score - second_score

    if top_score >= 8 or diff >= 5:
        confidence = "alta"
    elif top_score >= 4 or diff >= 2:
        confidence = "media"
    else:
        confidence = "baixa"

    return {
        "niche": top_niche,
        "confidence": confidence,
        "scores": niche_scores,
    }
