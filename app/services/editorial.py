import re
import unicodedata
from typing import Any


STOPWORDS = {
    "a", "as", "o", "os", "de", "do", "da", "dos", "das", "e", "em", "no", "na",
    "nos", "nas", "um", "uma", "uns", "umas", "que", "para", "por", "com", "sem",
    "como", "mais", "muito", "muita", "muitos", "muitas", "se", "eu", "voce", "você",
    "ele", "ela", "eles", "elas", "isso", "essa", "esse", "sobre", "ser", "estar",
}


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _sentences(text: str) -> list[str]:
    cleaned = _normalize_whitespace(text)
    if not cleaned:
        return []
    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    return [part.strip(" -") for part in parts if part.strip(" -")]


def _truncate(text: str, limit: int) -> str:
    cleaned = _normalize_whitespace(text)
    if len(cleaned) <= limit:
        return cleaned
    shortened = cleaned[:limit].rsplit(" ", 1)[0].strip()
    return f"{shortened}..."


def _top_keywords(text: str, max_items: int = 3) -> list[str]:
    tokens = re.findall(r"\w+", (text or "").lower(), flags=re.UNICODE)
    counts: dict[str, int] = {}
    for token in tokens:
        if len(token) < 4 or token in STOPWORDS:
            continue
        counts[token] = counts.get(token, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [token for token, _count in ordered[:max_items]]


def _slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", normalized.lower()).strip("-")
    return normalized or "clip"


def build_editorial_package(
    *,
    job_title: str | None,
    niche: str | None,
    mode: str,
    clip_id: int | None,
    start: float,
    end: float,
    text: str | None,
    reason: str | None,
    render_preset: str | None,
) -> dict[str, Any]:
    transcript_text = _normalize_whitespace(text or "")
    sentences = _sentences(transcript_text)
    intro = sentences[0] if sentences else transcript_text
    outro = sentences[1] if len(sentences) > 1 else ""
    niche_label = (niche or "geral").strip().lower()
    reason_label = _normalize_whitespace(reason or "")
    keywords = _top_keywords(transcript_text)

    title_core = _truncate(intro, 68) or _truncate(job_title or "Corte em destaque", 68)
    if mode == "short":
        headline = f"{title_core}"
    else:
        headline = f"{title_core} | Trecho completo"

    description_parts = []
    if job_title:
        description_parts.append(f"Corte gerado a partir de: {job_title}.")
    if reason_label:
        description_parts.append(f"Motivo do corte: {reason_label}.")
    if outro:
        description_parts.append(_truncate(outro, 110))
    description = _truncate(" ".join(description_parts).strip(), 180)

    hashtag_tokens = ["#cortes", "#videocut"]
    if mode == "short":
        hashtag_tokens.append("#shorts")
    else:
        hashtag_tokens.append("#video")
    if niche_label and niche_label != "geral":
        hashtag_tokens.append(f"#{_slugify(niche_label).replace('-', '')}")
    for keyword in keywords[:2]:
        hashtag_tokens.append(f"#{_slugify(keyword).replace('-', '')}")
    hashtags = " ".join(dict.fromkeys(hashtag_tokens))

    base_name_parts = [
        _slugify(job_title or "clip"),
        mode,
        _slugify(render_preset or "clean"),
        f"{int(start):04d}-{int(end):04d}",
    ]
    if clip_id is not None:
        base_name_parts.insert(1, f"clip-{clip_id}")

    return {
        "headline": headline,
        "description": description or _truncate(transcript_text, 180),
        "hashtags": hashtags,
        "suggested_filename": f"{'-'.join(base_name_parts)}.mp4",
        "keywords": keywords,
    }
