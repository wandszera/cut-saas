import json

from app.services.llm_provider import generate_json_with_llm


def _build_prompt(candidates: list[dict], mode: str) -> str:
    target_description = (
        "cortes curtos de até 3 minutos, priorizando gancho, clareza, independência de contexto e retenção"
        if mode == "short"
        else "trechos longos entre 5 e 15 minutos, priorizando desenvolvimento, contexto suficiente e progressão"
    )

    simplified_candidates = []
    for idx, candidate in enumerate(candidates, start=1):
        simplified_candidates.append(
            {
                "candidate_id": idx,
                "start": candidate["start"],
                "end": candidate["end"],
                "duration": candidate["duration"],
                "heuristic_score": candidate["score"],
                "opening_text": candidate.get("opening_text", "")[:400],
                "closing_text": candidate.get("closing_text", "")[:400],
                "text": candidate.get("text", "")[:1800],
            }
        )

    return f"""
Você é um editor de vídeo focado em cortes virais e claros.

Objetivo:
{target_description}

Regras:
- avalie apenas os candidatos fornecidos
- prefira trechos independentes, com começo forte e final utilizável
- penalize trechos que dependem de contexto visual ou conversa anterior
- use a nota heurística como sinal, mas não siga cegamente

Retorne apenas JSON válido no formato:
{{
  "selected": [
    {{
      "candidate_id": 1,
      "llm_score": 9.1,
      "why": "motivo curto",
      "title": "título curto sugerido",
      "hook": "gancho sugerido"
    }}
  ]
}}

Candidatos:
{json.dumps(simplified_candidates, ensure_ascii=False, indent=2)}
""".strip()


def analyze_candidates_with_llm(
    candidates: list[dict],
    mode: str = "short",
    *,
    heuristic_weight: float = 0.65,
    llm_weight: float = 0.35,
) -> list[dict]:
    if not candidates:
        return []

    prompt = _build_prompt(candidates, mode)
    parsed = generate_json_with_llm(prompt, timeout=45.0)
    if not isinstance(parsed, dict):
        raise RuntimeError("Resposta da LLM para rerank não veio em objeto JSON")

    enriched = []
    for item in parsed.get("selected", []):
        candidate_id = item.get("candidate_id")
        if not isinstance(candidate_id, int):
            continue
        index = candidate_id - 1
        if index < 0 or index >= len(candidates):
            continue

        original = candidates[index]
        llm_score = float(item.get("llm_score", 0) or 0)
        hybrid_score = round(
            (float(original.get("score", 0)) * heuristic_weight) + (llm_score * llm_weight),
            2,
        )
        why = (item.get("why") or "").strip()

        enriched.append(
            {
                **original,
                "llm_score": round(llm_score, 2),
                "llm_why": why,
                "llm_title": (item.get("title") or "").strip() or None,
                "llm_hook": (item.get("hook") or "").strip() or None,
                "base_score": original.get("base_score", original.get("score", 0)),
                "score": hybrid_score,
                "reason": f"{original.get('reason', '')}, revisão LLM: {why}".strip(", "),
            }
        )

    ranked_ids = {item["start"]: item for item in enriched}
    fallback = [candidate for candidate in candidates if candidate["start"] not in ranked_ids]
    combined = enriched + fallback
    return sorted(
        combined,
        key=lambda item: (
            item.get("llm_score") is not None,
            item.get("score", 0),
            item.get("base_score", item.get("score", 0)),
        ),
        reverse=True,
    )
