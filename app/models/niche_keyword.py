from sqlalchemy import Column, Integer, String, Float, DateTime
from sqlalchemy.sql import func
from app.db.database import Base
import re
from collections import Counter, defaultdict

from sqlalchemy.orm import Session

from app.models.candidate import Candidate



class NicheKeyword(Base):
    __tablename__ = "niche_keywords"

    id = Column(Integer, primary_key=True, index=True)

    niche = Column(String, nullable=False, index=True)
    keyword = Column(String, nullable=False, index=True)

    score = Column(Float, nullable=False, default=0.0)
    occurrences = Column(Integer, nullable=False, default=0)
    distinct_jobs = Column(Integer, nullable=False, default=0)

    source = Column(String, nullable=False, default="learned")  # base | learned
    status = Column(String, nullable=False, default="active")   # active | ignored

    created_at = Column(DateTime(timezone=True), server_default=func.now())


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
):
    if niche == "geral":
        return []

    candidates = (
        db.query(Candidate)
        .filter(
            Candidate.score >= min_candidate_score
        )
        .all()
    )

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


def get_learned_keywords_for_niche(db: Session, niche: str) -> list[str]:
    rows = (
        db.query(NicheKeyword)
        .filter(
            NicheKeyword.niche == niche,
            NicheKeyword.status == "active",
        )
        .order_by(NicheKeyword.score.desc())
        .all()
    )
    return [row.keyword for row in rows]