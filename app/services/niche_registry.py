import json
import re
from collections import Counter

from sqlalchemy.orm import Session

from app.models.niche_definition import NicheDefinition
from app.services.llm_provider import LLMRateLimitError, generate_json_with_llm
from app.services.niche_profiles import NICHE_PROFILES


DEFAULT_DYNAMIC_WEIGHTS = {
    "hook": 1.1,
    "clarity": 1.1,
    "closure": 1.0,
    "emotion": 1.0,
    "duration_fit": 1.0,
    "impact": 1.0,
    "continuity_penalty": 1.0,
    "format_bonus": 1.0,
    "niche_bonus": 1.0,
    "boundary": 1.0,
    "information_density": 1.0,
    "repetition_penalty": 1.0,
    "diversity_penalty": 1.0,
    "feedback_alignment": 1.0,
    "context_penalty": 1.0,
    "structure_bonus": 1.0,
    "cta_penalty": 1.0,
    "transcript_context": 1.0,
}

STOPWORDS = {
    "para", "com", "sem", "sobre", "entre", "onde", "quando", "porque",
    "como", "mais", "menos", "muito", "pouco", "uma", "uns", "umas", "dos",
    "das", "que", "isso", "essa", "esse", "esses", "essas", "tema", "nicho",
    "videos", "video", "conteudo", "conteúdo", "canal", "canais", "foco",
    "local", "geral", "brasil", "brasileiro", "brasileira",
}


def slugify_niche_name(name: str) -> str:
    normalized = (name or "").strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized)
    normalized = normalized.strip("-")
    return normalized or "nicho"


def _loads_json(raw_value: str | None, fallback):
    if not raw_value:
        return fallback
    try:
        loaded = json.loads(raw_value)
    except json.JSONDecodeError:
        return fallback
    return loaded


def _normalize_keywords(keywords: list[str] | None) -> list[str]:
    normalized = []
    seen = set()
    for item in keywords or []:
        keyword = re.sub(r"\s+", " ", str(item or "").strip().lower())
        if len(keyword) < 3:
            continue
        if keyword in seen:
            continue
        seen.add(keyword)
        normalized.append(keyword)
    return normalized[:24]


def _extract_candidate_keywords(text: str) -> list[str]:
    tokens = re.findall(r"[a-zA-ZÀ-ÿ0-9_-]{4,}", (text or "").lower())
    return [
        token for token in tokens
        if token not in STOPWORDS and not token.isdigit()
    ]


def _build_local_niche_suggestion(name: str, description: str | None = None) -> dict:
    source_text = f"{name} {description or ''}".strip()
    token_counts = Counter(_extract_candidate_keywords(source_text))
    keywords = []

    for token in [part for part in slugify_niche_name(name).split("-") if len(part) >= 4]:
        if token not in keywords:
            keywords.append(token)

    for token, _count in token_counts.most_common(16):
        if token not in keywords:
            keywords.append(token)

    if len(keywords) < 5:
        for token in [
            "estrategia",
            "audiencia",
            "resultado",
            "exemplo",
            "pratica",
            "mercado",
            "cliente",
            "processo",
            "dica",
            "analise",
        ]:
            if token not in keywords:
                keywords.append(token)
            if len(keywords) >= 8:
                break

    return {
        "description": (description or f"Nicho sugerido localmente para {name}.").strip(),
        "keywords": _normalize_keywords(keywords)[:12],
        "reason": (
            "Sugestão local criada automaticamente porque a LLM atingiu limite temporário de uso."
        ),
    }


def serialize_niche_definition(niche: NicheDefinition) -> dict:
    return {
        "id": niche.id,
        "name": niche.name,
        "slug": niche.slug,
        "description": niche.description,
        "keywords": _normalize_keywords(_loads_json(niche.keywords_json, [])),
        "weights": {**DEFAULT_DYNAMIC_WEIGHTS, **_loads_json(niche.weights_json, {})},
        "source": niche.source,
        "status": niche.status,
        "llm_notes": niche.llm_notes,
        "created_at": niche.created_at,
        "updated_at": niche.updated_at,
    }


def sync_builtin_niches(db: Session) -> None:
    existing = {
        row.slug: row
        for row in db.query(NicheDefinition).all()
    }
    changed = False

    for slug, profile in NICHE_PROFILES.items():
        row = existing.get(slug)
        keywords_json = json.dumps(_normalize_keywords(profile.get("keywords", [])), ensure_ascii=False)
        weights_json = json.dumps({**DEFAULT_DYNAMIC_WEIGHTS, **profile.get("weights", {})}, ensure_ascii=False)
        name = slug.replace("-", " ").title()

        if row is None:
            db.add(
                NicheDefinition(
                    name=name,
                    slug=slug,
                    description=f"Nicho base sincronizado do perfil heurístico '{slug}'.",
                    keywords_json=keywords_json,
                    weights_json=weights_json,
                    source="builtin",
                    status="active",
                )
            )
            changed = True
            continue

        updated = False
        if row.source != "builtin":
            row.source = "builtin"
            updated = True
        if row.name != name:
            row.name = name
            updated = True
        if row.keywords_json != keywords_json:
            row.keywords_json = keywords_json
            updated = True
        if row.weights_json != weights_json:
            row.weights_json = weights_json
            updated = True
        if row.status not in {"archived", "rejected", "pending"} and row.status != "active":
            row.status = "active"
            updated = True
        if updated:
            changed = True

    if changed:
        db.commit()


def list_niche_definitions(db: Session, *, include_inactive: bool = True) -> list[dict]:
    sync_builtin_niches(db)
    query = db.query(NicheDefinition).order_by(NicheDefinition.source.asc(), NicheDefinition.name.asc())
    rows = query.all()
    if not include_inactive:
        rows = [row for row in rows if row.status == "active"]
    return [serialize_niche_definition(row) for row in rows]


def get_niche_definition_by_slug(db: Session, slug: str) -> NicheDefinition | None:
    sync_builtin_niches(db)
    return db.query(NicheDefinition).filter(NicheDefinition.slug == slug).first()


def get_active_niche_profiles(db: Session) -> dict[str, dict]:
    niches = list_niche_definitions(db, include_inactive=False)
    profiles = {}
    for niche in niches:
        profiles[niche["slug"]] = {
            "keywords": niche["keywords"],
            "weights": niche["weights"],
            "name": niche["name"],
            "description": niche["description"],
            "source": niche["source"],
        }
    if "geral" not in profiles:
        profiles["geral"] = {
            "keywords": [],
            "weights": DEFAULT_DYNAMIC_WEIGHTS.copy(),
            "name": "Geral",
            "description": "Fallback padrão.",
            "source": "builtin",
        }
    return profiles


def get_niche_profile(db: Session, niche: str | None) -> dict:
    profiles = get_active_niche_profiles(db)
    normalized = (niche or "geral").strip().lower()
    return profiles.get(normalized, profiles["geral"])


def _build_niche_suggestion_prompt(name: str, description: str | None) -> str:
    return f"""
Você está ajudando a configurar um motor heurístico de cortes de vídeo.

Nicho proposto: {name}
Contexto adicional: {description or "sem contexto adicional"}

Retorne apenas JSON válido neste formato:
{{
  "description": "descrição curta do nicho",
  "keywords": ["palavra 1", "palavra 2", "palavra 3"],
  "reason": "explicação curta"
}}

Regras:
- gere entre 8 e 16 palavras-chave curtas e práticas
- foque em termos recorrentes que ajudam a reconhecer o nicho em título e transcrição
- evite palavras muito genéricas
- escreva em português
""".strip()


def suggest_keywords_for_new_niche(name: str, description: str | None = None) -> dict:
    prompt = _build_niche_suggestion_prompt(name, description)
    try:
        parsed = generate_json_with_llm(prompt, timeout=45.0)
        if not isinstance(parsed, dict):
            raise RuntimeError("Resposta do LLM para nicho não veio em objeto JSON")
    except LLMRateLimitError:
        return _build_local_niche_suggestion(name, description)

    keywords = _normalize_keywords(parsed.get("keywords", []))
    if len(keywords) < 5:
        raise RuntimeError("A sugestão da LLM não trouxe palavras-chave suficientes")

    return {
        "description": (parsed.get("description") or description or "").strip(),
        "keywords": keywords,
        "reason": (parsed.get("reason") or "").strip() or None,
    }


def create_pending_niche(
    db: Session,
    *,
    name: str,
    description: str | None = None,
) -> dict:
    slug = slugify_niche_name(name)
    existing = get_niche_definition_by_slug(db, slug)
    if existing and existing.status in {"active", "pending"}:
        raise ValueError("Já existe um nicho ativo ou pendente com esse nome")

    suggestion = suggest_keywords_for_new_niche(name=name, description=description)
    if existing is None:
        existing = NicheDefinition(slug=slug)
        db.add(existing)

    existing.name = name.strip()
    existing.description = suggestion["description"] or description or name.strip()
    existing.keywords_json = json.dumps(suggestion["keywords"], ensure_ascii=False)
    existing.weights_json = json.dumps(DEFAULT_DYNAMIC_WEIGHTS, ensure_ascii=False)
    existing.source = "custom"
    existing.status = "pending"
    existing.llm_notes = suggestion["reason"]
    db.commit()
    db.refresh(existing)
    return serialize_niche_definition(existing)


def approve_niche(db: Session, slug: str) -> dict:
    niche = get_niche_definition_by_slug(db, slug)
    if niche is None:
        raise ValueError("Nicho não encontrado")
    niche.status = "active"
    db.commit()
    db.refresh(niche)
    return serialize_niche_definition(niche)


def reject_niche(db: Session, slug: str) -> dict:
    niche = get_niche_definition_by_slug(db, slug)
    if niche is None:
        raise ValueError("Nicho não encontrado")
    niche.status = "rejected"
    db.commit()
    db.refresh(niche)
    return serialize_niche_definition(niche)


def archive_niche(db: Session, slug: str) -> dict:
    niche = get_niche_definition_by_slug(db, slug)
    if niche is None:
        raise ValueError("Nicho não encontrado")
    if niche.slug == "geral":
        raise ValueError("O nicho geral não pode ser excluído")
    niche.status = "archived"
    db.commit()
    db.refresh(niche)
    return serialize_niche_definition(niche)
