import re
from collections import Counter

from app.services.niche_profiles import NICHE_PROFILES


DEFAULT_WEIGHTS = {
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


def _get_niche_weights(niche: str | None, niche_profile: dict | None = None) -> dict:
    niche = (niche or "geral").lower().strip()
    profile = niche_profile or NICHE_PROFILES.get(niche, NICHE_PROFILES["geral"])
    return {**DEFAULT_WEIGHTS, **profile["weights"]}


def _niche_keyword_bonus_with_learned(
    full_text: str,
    niche: str | None,
    learned_keywords: list[str] | None = None,
    niche_profile: dict | None = None,
) -> tuple[float, list[str]]:
    niche = (niche or "geral").lower().strip()
    profile = niche_profile or NICHE_PROFILES.get(niche, NICHE_PROFILES["geral"])
    base_keywords = profile.get("keywords", [])
    learned_keywords = learned_keywords or []

    text = full_text.lower()

    found_base = sum(1 for kw in base_keywords if kw in text)
    found_learned = sum(1 for kw in learned_keywords if kw in text)

    bonus = 0.0
    reasons = []

    if found_base > 0:
        bonus += min(found_base * 0.6, 3.0)
        reasons.append(f"aderência ao nicho {niche}")

    if found_learned > 0:
        bonus += min(found_learned * 0.4, 2.0)
        reasons.append(f"aderência a vocabulário aprendido de {niche}")

    return bonus, reasons


HOOK_KEYWORDS = [
    "o problema",
    "o erro",
    "o segredo",
    "ninguém",
    "nunca",
    "sempre",
    "você precisa",
    "por que",
    "como",
    "a verdade",
    "o maior",
    "o pior",
    "sabe o que",
    "deixa eu te falar",
    "presta atenção",
    "vou te mostrar",
    "vou te explicar",
    "olha isso",
]

IMPACT_KEYWORDS = [
    "erro", "segredo", "verdade", "problema", "ninguém", "nunca",
    "sempre", "motivo", "absurdo", "diferença", "maior", "melhor",
    "pior", "precisa", "deveria", "funciona", "fracasso", "sucesso",
    "dinheiro", "resultado", "crescer", "falhar", "perigo",
]

EMOTION_KEYWORDS = [
    "medo", "raiva", "dor", "feliz", "triste", "chocante",
    "inacreditável", "polêmico", "surpreendente", "difícil",
    "urgente", "absurdo", "sério", "forte",
]

CLOSURE_KEYWORDS = [
    "por isso",
    "então",
    "ou seja",
    "no fim",
    "a conclusão",
    "é por isso",
    "esse é o ponto",
    "essa é a questão",
    "resumindo",
    "em resumo",
]

WEAK_START_PATTERNS = [
    "é...",
    "ah",
    "hum",
    "hã",
    "então assim",
    "tipo assim",
]

BROKEN_END_PATTERNS = [
    "e aí",
    "entendeu",
    "né",
    "tá",
    "assim",
]

FILLER_WORDS = {
    "tipo", "assim", "né", "cara", "mano", "tá", "aham", "hum", "é", "ai", "aí",
}

CONTEXT_DEPENDENCY_PATTERNS = [
    "isso aqui",
    "isso ai",
    "isso aí",
    "isso que eu falei",
    "isso que eu mostrei",
    "isso que aconteceu",
    "essa parte",
    "essa ideia",
    "essa cena",
    "esse trecho",
    "esse ponto",
    "esse caso",
    "esse contexto",
    "esse video",
    "esse vídeo",
    "como eu falei",
    "como falei",
    "como eu disse",
    "como disse",
    "ali em cima",
    "aqui embaixo",
    "nessa tela",
    "desse jeito",
    "dessa forma",
    "olhando isso",
    "vendo isso",
]

INFORMATIVE_OPENING_PATTERNS = [
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
    "eu estava pensando",
]

STRONG_OPENING_PATTERNS = [
    "por que",
    "o erro",
    "o segredo",
    "o problema",
    "ninguém",
    "quase todo mundo",
    "a verdade",
    "o maior",
    "o pior",
    "3 passos",
    "três passos",
    "3 erros",
    "três erros",
]

STRUCTURE_PATTERNS = [
    "primeiro",
    "segundo",
    "terceiro",
    "passo",
    "3 passos",
    "três passos",
    "lista",
    "resumindo",
    "em resumo",
]

CTA_PATTERNS = [
    "se inscreve",
    "se inscrever",
    "deixa o like",
    "curte o video",
    "curte o vídeo",
    "compartilha",
    "me segue",
    "segue pra mais",
    "link na bio",
]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _word_count(text: str) -> int:
    return len(_normalize(text).split())


def _tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", _normalize(text), flags=re.UNICODE)


def _contains_any(text: str, keywords: list[str]) -> int:
    normalized = _normalize(text)
    return sum(1 for kw in keywords if kw in normalized)


def _duration_fit_score(duration: float, mode: str, calibration_profile: dict | None = None) -> tuple[float, str]:
    if mode == "long":
        target = 600.0
        min_d = 300.0
        max_d = 900.0

        if duration < min_d or duration > max_d:
            return -6.0, "fora da faixa ideal de vídeo longo"

        diff = abs(duration - target)
        if diff <= 60:
            return 5.0, "muito bem encaixado na duração alvo"
        if diff <= 120:
            return 4.0, "bem encaixado na duração alvo"
        if diff <= 180:
            return 3.0, "duração aceitável"
        return 2.0, "duração um pouco distante do alvo"

    target = 90.0
    min_d = 30.0
    max_d = 180.0

    if duration < min_d or duration > max_d:
        return -6.0, "fora da faixa ideal de short"

    diff = abs(duration - target)
    preferred_short_max = float((calibration_profile or {}).get("preferred_short_max_seconds", 120.0) or 120.0)
    if duration > preferred_short_max:
        return 0.5, "longo demais para short competitivo"
    if diff <= 15:
        return 5.0, "muito bem encaixado na duração alvo"
    if diff <= 30:
        return 4.0, "bem encaixado na duração alvo"
    if diff <= 50:
        return 3.0, "duração aceitável"
    return 2.0, "duração um pouco distante do alvo"


def _hook_score(opening_text: str, mode: str) -> tuple[float, list[str]]:
    text = _normalize(opening_text)
    score = 0.0
    reasons = []

    if "?" in opening_text:
        score += 2.0
        reasons.append("abertura com pergunta")

    hooks_found = _contains_any(text, HOOK_KEYWORDS)
    if hooks_found:
        score += min(2.5, hooks_found * 1.2)
        reasons.append("abertura com gancho")

    words = _word_count(text)
    if 6 <= words <= 28:
        score += 1.5
        reasons.append("abertura objetiva")

    weak_start = any(text.startswith(pattern) for pattern in WEAK_START_PATTERNS)
    if weak_start:
        score -= 1.5
        reasons.append("abertura fraca")

    if mode == "short" and words > 35:
        score -= 1.0
        reasons.append("abertura longa demais para short")

    return score, reasons


def _opening_strength_score(candidate: dict, mode: str, calibration_profile: dict | None = None) -> tuple[float, list[str]]:
    opening = _normalize(candidate.get("opening_text", ""))
    text = _normalize(candidate.get("text", ""))
    informative_multiplier = float((calibration_profile or {}).get("informative_opening_multiplier", 1.0) or 1.0)
    score = 0.0
    reasons = []

    if any(opening.startswith(pattern) for pattern in INFORMATIVE_OPENING_PATTERNS):
        score -= 1.8 * informative_multiplier
        reasons.append("abertura mais informativa do que forte")

    if any(pattern in opening for pattern in STRONG_OPENING_PATTERNS):
        score += 1.6
        reasons.append("abertura com tensão ou promessa forte")

    if opening.startswith(("ele ", "ela ", "isso ", "esse ", "essa ", "aí ", "ai ")) and not candidate.get("starts_clean"):
        score -= 1.2
        reasons.append("abertura depende de referência anterior")

    if mode == "short" and _word_count(opening) >= 24 and "?" not in opening:
        score -= 0.6
        reasons.append("abertura demora para chegar no ponto")

    if re.search(r"\b(mas|só que|so que|o problema|o erro)\b", opening):
        score += 0.6
        reasons.append("abertura cria contraste")

    if re.search(r"\b(esse|essa|isso)\b", opening) and not re.search(r"\b(erro|problema|segredo|ponto|motivo|passo)\b", text):
        score -= 0.7
        reasons.append("abertura pouco autônoma")

    return score, reasons


def _clarity_score(full_text: str, opening_text: str, closing_text: str, mode: str) -> tuple[float, list[str]]:
    score = 0.0
    reasons = []

    total_words = _word_count(full_text)
    opening_words = _word_count(opening_text)
    closing_words = _word_count(closing_text)

    if mode == "short":
        if 35 <= total_words <= 220:
            score += 2.0
            reasons.append("volume bom para short")
        elif total_words < 20:
            score -= 2.5
            reasons.append("conteúdo curto demais")
        elif total_words > 320:
            score -= 1.0
            reasons.append("conteúdo denso demais para short")
    else:
        if 350 <= total_words <= 2500:
            score += 2.5
            reasons.append("volume bom para long")
        elif total_words < 220:
            score -= 2.0
            reasons.append("conteúdo curto para long")

    if opening_words >= 6:
        score += 0.8
        reasons.append("início com contexto mínimo")

    if closing_words >= 6:
        score += 0.8
        reasons.append("final com contexto mínimo")

    return score, reasons


def _content_strength_score(full_text: str) -> tuple[float, float, list[str]]:
    text = _normalize(full_text)
    score = 0.0
    emotion_score = 0.0
    reasons = []

    impact_found = _contains_any(text, IMPACT_KEYWORDS)
    emotion_found = _contains_any(text, EMOTION_KEYWORDS)

    if impact_found:
        score += min(4.0, impact_found * 0.8)
        reasons.append("conteúdo com impacto")

    if emotion_found:
        emotion_score += min(3.0, emotion_found * 0.7)
        reasons.append("carga emocional")

    if ":" in full_text or ";" in full_text:
        score += 0.4
        reasons.append("estrutura de fala mais densa")

    return score, emotion_score, reasons


def _closure_score(closing_text: str) -> tuple[float, list[str]]:
    text = _normalize(closing_text)
    score = 0.0
    reasons = []

    closure_found = _contains_any(text, CLOSURE_KEYWORDS)
    if closure_found:
        score += min(2.5, closure_found * 1.2)
        reasons.append("fechamento coerente")

    if _word_count(text) >= 8:
        score += 1.0
        reasons.append("final com substância")

    broken_end = any(text.endswith(pattern) for pattern in BROKEN_END_PATTERNS)
    if broken_end:
        score -= 1.2
        reasons.append("final fraco")

    return score, reasons


def _continuity_penalty(opening_text: str, closing_text: str) -> tuple[float, list[str]]:
    score = 0.0
    reasons = []

    opening = _normalize(opening_text)
    closing = _normalize(closing_text)

    if opening.startswith(("e ", "mas ", "porque ", "então ", "aí ")):
        score -= 1.8
        reasons.append("começa com cara de continuação")

    if closing.endswith(("porque", "mas", "então", "quando", "se")):
        score -= 1.8
        reasons.append("termina com cara de continuação")

    return score, reasons


def _format_bonus(opening_text: str, middle_text: str, mode: str) -> tuple[float, list[str]]:
    score = 0.0
    reasons = []

    opening = _normalize(opening_text)
    middle = _normalize(middle_text)

    if mode == "short":
        if any(term in opening for term in ["por que", "como", "o erro", "o segredo", "o problema"]):
            score += 1.5
            reasons.append("bom formato para short")
    else:
        if _word_count(middle) > 80:
            score += 1.5
            reasons.append("bom desenvolvimento para long")

    return score, reasons


def _boundary_score(candidate: dict) -> tuple[float, list[str]]:
    score = 0.0
    reasons = []
    pause_before = float(candidate.get("pause_before", 0.0) or 0.0)
    pause_after = float(candidate.get("pause_after", 0.0) or 0.0)

    if candidate.get("starts_clean"):
        score += 1.4
        reasons.append("começa em fronteira limpa")
    elif pause_before < 0.15:
        score -= 0.8
        reasons.append("entrada colada na fala anterior")

    if candidate.get("ends_clean"):
        score += 1.4
        reasons.append("termina em frase completa")
    elif pause_after < 0.15:
        score -= 0.8
        reasons.append("corte termina abruptamente")

    if pause_before >= 0.35:
        score += 0.8
        reasons.append("boa pausa de entrada")
    if pause_after >= 0.35:
        score += 0.8
        reasons.append("boa pausa de saída")

    return score, reasons


def _information_density_score(candidate: dict, mode: str) -> tuple[float, list[str]]:
    text = candidate.get("text", "")
    tokens = _tokenize(text)
    if not tokens:
        return -2.0, ["sem conteúdo suficiente"]

    score = 0.0
    reasons = []
    unique_ratio = len(set(tokens)) / max(len(tokens), 1)
    punctuation_hits = sum(text.count(mark) for mark in ["?", "!", ":", ";"])
    digits_hits = sum(char.isdigit() for char in text)
    segment_count = max(int(candidate.get("segments_count", 1) or 1), 1)
    words_per_segment = len(tokens) / segment_count

    if 0.55 <= unique_ratio <= 0.9:
        score += 1.8
        reasons.append("boa densidade lexical")
    elif unique_ratio < 0.4:
        score -= 1.2
        reasons.append("texto muito repetitivo")

    if punctuation_hits >= 2:
        score += 0.9
        reasons.append("estrutura argumentativa forte")

    if digits_hits >= 2:
        score += 0.6
        reasons.append("contém detalhe concreto")

    if mode == "short":
        if 8 <= words_per_segment <= 28:
            score += 0.8
            reasons.append("cadência boa por segmento")
    elif 12 <= words_per_segment <= 38:
        score += 0.8
        reasons.append("cadência boa por segmento")

    return score, reasons


def _repetition_penalty(full_text: str) -> tuple[float, list[str]]:
    tokens = _tokenize(full_text)
    if not tokens:
        return -1.5, ["texto insuficiente"]

    score = 0.0
    reasons = []
    token_counts = Counter(tokens)
    repeated_fillers = sum(token_counts[word] for word in FILLER_WORDS if token_counts[word] > 1)
    dominant_ratio = max(token_counts.values()) / len(tokens)

    if repeated_fillers >= 4:
        score -= 1.4
        reasons.append("muitos vícios de fala")

    if dominant_ratio > 0.12:
        score -= 0.8
        reasons.append("vocabulário pouco variado")

    return score, reasons


def _context_dependency_penalty(candidate: dict, calibration_profile: dict | None = None) -> tuple[float, list[str]]:
    score = 0.0
    reasons = []
    text = _normalize(candidate.get("text", ""))
    opening = _normalize(candidate.get("opening_text", ""))
    context_multiplier = float((calibration_profile or {}).get("context_penalty_multiplier", 1.0) or 1.0)

    context_hits = _contains_any(text, CONTEXT_DEPENDENCY_PATTERNS)
    if context_hits:
        score -= min(2.4 * context_multiplier, context_hits * 0.8 * context_multiplier)
        reasons.append("trecho dependente de contexto externo")

    if opening.startswith(("então", "entao", "bom", "agora", "daí", "dai")) and not candidate.get("starts_clean"):
        score -= 0.8 * context_multiplier
        reasons.append("abertura com transição contextual")

    if not candidate.get("starts_clean") and opening.startswith(("ele ", "ela ", "eles ", "elas ", "isso ", "esse ", "essa ", "aqui ")):
        score -= 1.0 * context_multiplier
        reasons.append("começa sem referente claro")

    if _word_count(opening) < 7 and not candidate.get("starts_clean"):
        score -= 0.6 * context_multiplier
        reasons.append("abertura curta demais para se sustentar sozinha")

    return score, reasons


def _structure_bonus(candidate: dict, mode: str) -> tuple[float, list[str]]:
    score = 0.0
    reasons = []
    text = _normalize(candidate.get("text", ""))

    structure_hits = _contains_any(text, STRUCTURE_PATTERNS)
    if structure_hits:
        score += min(2.0, structure_hits * 0.7)
        reasons.append("estrutura clara de explicação")

    if text.count("?") >= 1 and mode == "short":
        score += 0.4
        reasons.append("curiosidade sustentada")

    if re.search(r"\b\d+\b", text) and any(word in text for word in ("passo", "erro", "motivo", "forma", "jeito")):
        score += 0.8
        reasons.append("promessa específica")

    return score, reasons


def _cta_penalty(full_text: str, closing_text: str) -> tuple[float, list[str]]:
    score = 0.0
    reasons = []
    text = _normalize(full_text)
    closing = _normalize(closing_text)

    cta_hits = _contains_any(text, CTA_PATTERNS)
    if cta_hits:
        score -= min(2.0, cta_hits * 1.0)
        reasons.append("fecha com CTA promocional")

    if any(pattern in closing for pattern in CTA_PATTERNS):
        score -= 0.8
        reasons.append("encerramento mais promocional do que editorial")

    return score, reasons


def _transcript_context_score(candidate: dict, transcript_insights: dict | None) -> tuple[float, list[str]]:
    if not transcript_insights:
        return 0.0, []

    score = 0.0
    reasons = []
    text = _normalize(candidate.get("text", ""))
    start = float(candidate.get("start", 0.0) or 0.0)
    end = float(candidate.get("end", 0.0) or 0.0)

    priority_keywords = transcript_insights.get("priority_keywords", []) or []
    avoid_patterns = transcript_insights.get("avoid_patterns", []) or []
    promising_ranges = transcript_insights.get("promising_ranges", []) or []

    keyword_hits = sum(1 for keyword in priority_keywords[:8] if _normalize(str(keyword)) in text)
    if keyword_hits:
        score += min(2.0, keyword_hits * 0.5)
        reasons.append("alinhado aos tópicos prioritários da transcrição")

    avoid_hits = sum(1 for pattern in avoid_patterns[:8] if _normalize(str(pattern)) in text)
    if avoid_hits:
        score -= min(2.0, avoid_hits * 0.6)
        reasons.append("bate em padrão a evitar da transcrição")

    for item in promising_ranges[:5]:
        try:
            range_start = float(item.get("start_hint_seconds", 0))
            range_end = float(item.get("end_hint_seconds", 0))
        except (TypeError, ValueError, AttributeError):
            continue
        overlap_start = max(start, range_start)
        overlap_end = min(end, range_end)
        overlap = max(0.0, overlap_end - overlap_start)
        if overlap <= 0:
            continue
        shorter = min(max(end - start, 1.0), max(range_end - range_start, 1.0))
        overlap_ratio = overlap / shorter
        if overlap_ratio >= 0.5:
            score += 1.2
            reasons.append("coincide com trecho promissor da análise global")
            break

    return score, reasons


def _feedback_alignment_score(
    candidate_metrics: dict[str, float],
    full_text: str,
    feedback_profile: dict | None,
) -> tuple[float, list[str]]:
    if not feedback_profile or not feedback_profile.get("min_samples_reached"):
        return 0.0, []

    score = 0.0
    reasons = []
    positive_means = feedback_profile.get("positive_means", {})
    negative_means = feedback_profile.get("negative_means", {})

    comparable_metrics = [
        "hook_score",
        "clarity_score",
        "closure_score",
        "emotion_score",
        "duration_fit_score",
        "duration",
    ]

    aligned_metrics = 0
    for metric in comparable_metrics:
        candidate_value = float(candidate_metrics.get(metric, 0.0))
        positive_value = float(positive_means.get(metric, 0.0))
        negative_value = float(negative_means.get(metric, 0.0))
        positive_distance = abs(candidate_value - positive_value)
        negative_distance = abs(candidate_value - negative_value) if negative_value else positive_distance + 0.5

        if positive_distance + 0.15 < negative_distance:
            score += 0.45
            aligned_metrics += 1
        elif negative_value and negative_distance + 0.15 < positive_distance:
            score -= 0.35

    if aligned_metrics >= 2:
        reasons.append("alinhado com feedback positivo")

    successful_keywords = feedback_profile.get("successful_keywords", [])
    text = full_text.lower()
    keyword_hits = sum(1 for keyword in successful_keywords[:8] if keyword in text)
    if keyword_hits:
        score += min(keyword_hits * 0.2, 1.0)
        reasons.append("vocabulário parecido com cortes aprovados")

    return score, reasons


def _score_candidate(
    candidate: dict,
    *,
    mode: str,
    niche: str,
    weights: dict,
    learned_keywords: list[str] | None,
    feedback_profile: dict | None,
    transcript_insights: dict | None,
    niche_profile: dict | None,
    calibration_profile: dict | None,
) -> dict:
    duration = float(candidate.get("duration", 0))
    text = candidate.get("text", "")
    opening_text = candidate.get("opening_text", "")
    middle_text = candidate.get("middle_text", "")
    closing_text = candidate.get("closing_text", "")

    total_score = 0.0
    reasons = []

    duration_fit_score, duration_reasons = _duration_fit_score(duration, mode, calibration_profile)
    total_score += duration_fit_score * weights["duration_fit"]
    reasons.append(duration_reasons)

    hook_score, hook_reasons = _hook_score(opening_text, mode)
    total_score += hook_score * weights["hook"]
    reasons.extend(hook_reasons)

    opening_strength_score, opening_strength_reasons = _opening_strength_score(candidate, mode, calibration_profile)
    total_score += opening_strength_score * weights["hook"]
    reasons.extend(opening_strength_reasons)

    clarity_score, clarity_reasons = _clarity_score(text, opening_text, closing_text, mode)
    total_score += clarity_score * weights["clarity"]
    reasons.extend(clarity_reasons)

    impact_score, emotion_score, content_reasons = _content_strength_score(text)
    total_score += impact_score * weights["impact"]
    total_score += emotion_score * weights["emotion"]
    reasons.extend(content_reasons)

    closure_score, closure_reasons = _closure_score(closing_text)
    total_score += closure_score * weights["closure"]
    reasons.extend(closure_reasons)

    continuity_penalty, continuity_reasons = _continuity_penalty(opening_text, closing_text)
    total_score += continuity_penalty * weights["continuity_penalty"]
    reasons.extend(continuity_reasons)

    format_bonus, format_reasons = _format_bonus(opening_text, middle_text, mode)
    total_score += format_bonus * weights["format_bonus"]
    reasons.extend(format_reasons)

    boundary_score, boundary_reasons = _boundary_score(candidate)
    total_score += boundary_score * weights["boundary"]
    reasons.extend(boundary_reasons)

    information_density_score, information_reasons = _information_density_score(candidate, mode)
    total_score += information_density_score * weights["information_density"]
    reasons.extend(information_reasons)

    repetition_penalty, repetition_reasons = _repetition_penalty(text)
    total_score += repetition_penalty * weights["repetition_penalty"]
    reasons.extend(repetition_reasons)

    context_penalty, context_reasons = _context_dependency_penalty(candidate, calibration_profile)
    total_score += context_penalty * weights["context_penalty"]
    reasons.extend(context_reasons)

    structure_bonus, structure_reasons = _structure_bonus(candidate, mode)
    total_score += structure_bonus * weights["structure_bonus"]
    reasons.extend(structure_reasons)

    cta_penalty, cta_reasons = _cta_penalty(text, closing_text)
    total_score += cta_penalty * weights["cta_penalty"]
    reasons.extend(cta_reasons)

    transcript_context_score, transcript_context_reasons = _transcript_context_score(
        candidate,
        transcript_insights,
    )
    total_score += transcript_context_score * weights["transcript_context"]
    reasons.extend(transcript_context_reasons)

    niche_bonus, niche_reasons = _niche_keyword_bonus_with_learned(
        text,
        niche,
        learned_keywords,
        niche_profile=niche_profile,
    )
    total_score += niche_bonus * weights["niche_bonus"]
    reasons.extend(niche_reasons)

    candidate_metrics = {
        "hook_score": hook_score,
        "opening_strength_score": opening_strength_score,
        "clarity_score": clarity_score,
        "closure_score": closure_score,
        "emotion_score": emotion_score,
        "duration_fit_score": duration_fit_score,
        "duration": duration,
    }
    feedback_alignment_score, feedback_reasons = _feedback_alignment_score(
        candidate_metrics,
        text,
        feedback_profile,
    )
    total_score += feedback_alignment_score * weights["feedback_alignment"]
    reasons.extend(feedback_reasons)

    if _word_count(text) < 15:
        total_score -= 2.0
        reasons.append("texto insuficiente")

    if hook_score >= 2 and closure_score >= 1:
        total_score += 1.0
        reasons.append("trecho com começo e fim mais fortes")

    return {
        **candidate,
        "base_score": round(total_score, 2),
        "score": round(total_score, 2),
        "reason": ", ".join(dict.fromkeys(reasons)),
        "hook_score": round(hook_score, 2),
        "opening_strength_score": round(opening_strength_score, 2),
        "clarity_score": round(clarity_score, 2),
        "closure_score": round(closure_score, 2),
        "emotion_score": round(emotion_score, 2),
        "duration_fit_score": round(duration_fit_score, 2),
        "impact_score": round(impact_score, 2),
        "continuity_penalty": round(continuity_penalty, 2),
        "format_bonus": round(format_bonus, 2),
        "niche_bonus": round(niche_bonus, 2),
        "boundary_score": round(boundary_score, 2),
        "information_density_score": round(information_density_score, 2),
        "repetition_penalty": round(repetition_penalty, 2),
        "context_penalty": round(context_penalty, 2),
        "structure_bonus": round(structure_bonus, 2),
        "cta_penalty": round(cta_penalty, 2),
        "transcript_context_score": round(transcript_context_score, 2),
        "feedback_alignment_score": round(feedback_alignment_score, 2),
        "diversity_penalty": 0.0,
        "niche_used": niche,
    }


def _text_similarity(left: str, right: str) -> float:
    left_tokens = set(_tokenize(left))
    right_tokens = set(_tokenize(right))
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    return intersection / union if union else 0.0


def _time_overlap_ratio(left: dict, right: dict) -> float:
    overlap_start = max(float(left.get("start", 0)), float(right.get("start", 0)))
    overlap_end = min(float(left.get("end", 0)), float(right.get("end", 0)))
    overlap = max(0.0, overlap_end - overlap_start)
    shorter = min(float(left.get("duration", 0) or 0), float(right.get("duration", 0) or 0))
    if shorter <= 0:
        return 0.0
    return overlap / shorter


def _apply_diversity_reranking(
    candidates: list[dict],
    diversity_weight: float,
    calibration_profile: dict | None = None,
) -> list[dict]:
    selected: list[dict] = []
    remaining = sorted(candidates, key=lambda item: item["base_score"], reverse=True)
    diversity_multiplier = float((calibration_profile or {}).get("diversity_penalty_multiplier", 1.0) or 1.0)

    while remaining:
        best_choice = None
        best_effective_score = None

        for candidate in remaining:
            max_overlap = max((_time_overlap_ratio(candidate, kept) for kept in selected), default=0.0)
            max_similarity = max((_text_similarity(candidate.get("text", ""), kept.get("text", "")) for kept in selected), default=0.0)
            opening_similarity = max(
                (
                    _text_similarity(candidate.get("opening_text", ""), kept.get("opening_text", ""))
                    for kept in selected
                ),
                default=0.0,
            )
            diversity_penalty = (max_overlap * 2.8) + (max_similarity * 1.5) + (opening_similarity * 1.2)
            if max_overlap >= 0.7 and max_similarity >= 0.72:
                diversity_penalty += 1.2
            if opening_similarity >= 0.75:
                diversity_penalty += 1.6
            if max_overlap >= 0.7 and opening_similarity >= 0.75:
                diversity_penalty += 1.6
            diversity_penalty = round(diversity_penalty, 2)
            effective_score = candidate["base_score"] - (diversity_penalty * diversity_weight * diversity_multiplier)

            if best_effective_score is None or effective_score > best_effective_score:
                best_choice = {
                    **candidate,
                    "score": round(effective_score, 2),
                    "diversity_penalty": round(diversity_penalty, 2),
                }
                best_effective_score = effective_score

        if best_choice is None:
            break

        if best_choice["diversity_penalty"] > 0:
            best_choice["reason"] += ", reduz redundância com outros cortes"

        selected.append(best_choice)
        remaining = [
            candidate for candidate in remaining
            if not (
                candidate["start"] == best_choice["start"]
                and candidate["end"] == best_choice["end"]
                and candidate.get("text", "") == best_choice.get("text", "")
            )
        ]

    return sorted(selected, key=lambda item: item["score"], reverse=True)


def score_candidates(
    candidates: list[dict],
    mode: str = "short",
    niche: str = "geral",
    learned_keywords: list[str] | None = None,
    feedback_profile: dict | None = None,
    transcript_insights: dict | None = None,
    niche_profile: dict | None = None,
    calibration_profile: dict | None = None,
) -> list[dict]:
    weights = _get_niche_weights(niche, niche_profile=niche_profile)
    scored = [
        _score_candidate(
            candidate,
            mode=mode,
            niche=niche,
            weights=weights,
            learned_keywords=learned_keywords,
            feedback_profile=feedback_profile,
            transcript_insights=transcript_insights,
            niche_profile=niche_profile,
            calibration_profile=calibration_profile,
        )
        for candidate in candidates
    ]
    return _apply_diversity_reranking(scored, weights["diversity_penalty"], calibration_profile)
