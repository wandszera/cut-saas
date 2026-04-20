import json

from app.core.config import settings
from app.services.llm_provider import generate_json_with_llm


def _build_transcript_prompt(title: str | None, transcript_text: str) -> str:
    safe_text = (transcript_text or "")[:12000]
    return f"""
Você é um estrategista editorial de cortes de vídeo.

Analise a transcrição abaixo e retorne apenas JSON válido com:
- main_topics: lista curta
- viral_angles: lista curta
- priority_keywords: lista curta de palavras/expressões a priorizar
- avoid_patterns: lista curta de padrões a evitar
- promising_ranges: lista de objetos com start_hint_seconds, end_hint_seconds e why

Regras:
- use apenas a transcrição
- se não houver tempo exato, estime ranges aproximados
- seja conservador e útil para um motor heurístico

Formato:
{{
  "main_topics": ["tema 1"],
  "viral_angles": ["ângulo 1"],
  "priority_keywords": ["palavra 1"],
  "avoid_patterns": ["padrão 1"],
  "promising_ranges": [
    {{"start_hint_seconds": 30, "end_hint_seconds": 95, "why": "gancho forte"}}
  ]
}}

Título: {title or "Sem título"}

Transcrição:
{safe_text}
""".strip()


def analyze_transcript_context(title: str | None, transcript_text: str) -> dict:
    if not settings.llm_rerank_enabled:
        return {}

    prompt = _build_transcript_prompt(title, transcript_text)
    parsed = generate_json_with_llm(prompt, timeout=settings.llm_timeout_seconds)
    if not isinstance(parsed, dict):
        return {}
    return parsed
