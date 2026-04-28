from app.models.clip import Clip


PUBLICATION_STATUS_LABELS = {
    "draft": "Rascunho",
    "ready": "Pronto",
    "published": "Publicado",
    "discarded": "Descartado",
}


def normalize_publication_status(status: str | None) -> str:
    normalized = (status or "").strip().lower()
    if normalized not in PUBLICATION_STATUS_LABELS:
        raise ValueError("Status de publicacao invalido")
    return normalized


def build_clip_publication_package(clip: Clip) -> dict:
    title = (clip.headline or "").strip()
    description = (clip.description or "").strip()
    hashtags = _split_hashtags(clip.hashtags)
    caption_parts = [part for part in [description, " ".join(hashtags)] if part]
    status = normalize_publication_status(clip.publication_status)
    return {
        "title": title,
        "description": description,
        "hashtags": hashtags,
        "caption": "\n\n".join(caption_parts),
        "suggested_filename": clip.suggested_filename,
        "publication_status": status,
        "status_label": PUBLICATION_STATUS_LABELS[status],
    }


def _split_hashtags(value: str | None) -> list[str]:
    if not value:
        return []
    return [item for item in value.replace(",", " ").split() if item]
