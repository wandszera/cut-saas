import json
from typing import Any

from openai import OpenAI

from app.core.config import settings


def _build_prompt(candidates: list[dict], mode: str) -> str:
    target_description = (
        "cortes curtos de até 3 minutos, preferindo trechos fortes, com gancho inicial alto, retenção e potencial para reels/tiktok/shorts"
        if mode == "short"
        else "trechos longos entre 5 e 15 minutos, com desenvolvimento de ideia, contexto, progressão e valor para vídeo longo"
    )

    simplified_candidates = []
    for idx, c in enumerate(candidates, start=1):
        simplified_candidates.append(
            {
                "candidate_id": idx,
                "start": c["start"],
                "end": c["end"],
                "duration": c["duration"],
                "heuristic_score": c["score"],
                "text": c["text"][:4000],  # proteção simples
                "opening_text": c.get("opening_text", ""),
                "closing_text": c.get("closing_text", ""),
            }
        )

    return f"""
Você é um editor especialista em cortes virais e retenção.

Sua tarefa é analisar candidatos de cortes extraídos de uma transcrição e selecionar os melhores.

Objetivo deste modo:
{target_description}

Critérios importantes:
- força do gancho inicial
- clareza do tema
- potencial de retenção
- presença de conflito, opinião, emoção, curiosidade ou aprendizado forte
- sensação de trecho "com começo, meio e fim"
- adequação da duração ao formato
- evitar trechos confusos, dependentes demais de contexto externo ou com início fraco

Para cada candidato selecionado, retorne:
- candidate_id
- llm_score (0 a 10)
- cut_type (Polêmico, Emocional, Didático, Reflexivo, Viral)
- why
- hook
- title
- format_recommendation (short, long, both)

Retorne APENAS JSON válido no formato:
{{
  "selected": [
    {{
      "candidate_id": 1,
      "llm_score": 9.2,
      "cut_type": "Polêmico",
      "why": "Explicação curta",
      "hook": "Frase de abertura forte",
      "title": "Título sugerido",
      "format_recommendation": "short"
    }}
  ]
}}

Candidatos:
{json.dumps(simplified_candidates, ensure_ascii=False, indent=2)}
""".strip()


def analyze_candidates_with_llm(candidates: list[dict], mode: str = "short") -> list[dict]:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY não configurada no .env")

    if not candidates:
        return []

    client = OpenAI(api_key=settings.openai_api_key)

    prompt = _build_prompt(candidates, mode)

    response = client.responses.create(
        model=settings.llm_model,
        input=prompt,
        temperature=0.2,
    )

    text_output = response.output_text.strip()

    try:
        parsed = json.loads(text_output)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Resposta do LLM não veio em JSON válido: {text_output}") from e

    selected = parsed.get("selected", [])
    enriched = []

    for item in selected:
        candidate_id = item.get("candidate_id")
        if not isinstance(candidate_id, int):
            continue

        index = candidate_id - 1
        if index < 0 or index >= len(candidates):
            continue

        original = candidates[index]

        enriched.append({
            **original,
            "llm_score": item.get("llm_score"),
            "cut_type": item.get("cut_type"),
            "why": item.get("why"),
            "hook": item.get("hook"),
            "title": item.get("title"),
            "format_recommendation": item.get("format_recommendation"),
        })

    enriched.sort(
        key=lambda x: (
            x.get("llm_score") is not None,
            x.get("llm_score", 0),
            x.get("score", 0),
        ),
        reverse=True
    )

    return enriched