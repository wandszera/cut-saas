def _duration_score(duration: float, mode: str) -> tuple[float, str]:
    if mode == "long":
        target = 600.0  # 10 min
        min_d = 300.0
        max_d = 900.0

        if not (min_d <= duration <= max_d):
            return -5.0, "fora da faixa longa"

        diff = abs(duration - target)

        if diff <= 60:
            return 5.0, "muito próximo da duração ideal"
        if diff <= 120:
            return 4.0, "próximo da duração ideal"
        if diff <= 180:
            return 3.0, "boa duração"
        return 2.0, "duração aceitável"

    # short
    target = 120.0
    min_d = 30.0
    max_d = 180.0

    if not (min_d <= duration <= max_d):
        return -5.0, "fora da faixa curta"

    diff = abs(duration - target)

    if diff <= 20:
        return 5.0, "muito próximo da duração ideal"
    if diff <= 40:
        return 4.0, "próximo da duração ideal"
    if diff <= 60:
        return 3.0, "boa duração"
    return 2.0, "duração aceitável"


def _opening_hook_score(opening_text: str) -> tuple[float, list[str]]:
    text = opening_text.lower()
    score = 0.0
    reasons = []

    hook_keywords = [
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
    ]

    if "?" in text:
        score += 2.0
        reasons.append("abertura com pergunta")

    for kw in hook_keywords:
        if kw in text:
            score += 2.0
            reasons.append("abertura com gancho")
            break

    word_count = len(text.split())
    if 8 <= word_count <= 35:
        score += 1.5
        reasons.append("abertura objetiva")

    return score, reasons


def _content_strength_score(full_text: str) -> tuple[float, list[str]]:
    text = full_text.lower()
    score = 0.0
    reasons = []

    strong_keywords = [
        "erro", "segredo", "verdade", "problema", "ninguém", "nunca",
        "sempre", "motivo", "absurdo", "diferença", "maior", "melhor",
        "pior", "precisa", "deveria", "funciona", "fracasso", "sucesso"
    ]

    emotional_keywords = [
        "medo", "raiva", "dor", "feliz", "triste", "chocante",
        "inacreditável", "polêmico", "surpreendente", "difícil"
    ]

    curiosity_keywords = [
        "por quê", "por que", "como", "o que", "qual", "imagina",
        "descobri", "acontece", "a questão", "o ponto"
    ]

    found_strong = sum(1 for word in strong_keywords if word in text)
    found_emotion = sum(1 for word in emotional_keywords if word in text)
    found_curiosity = sum(1 for word in curiosity_keywords if word in text)

    if found_strong:
        score += min(found_strong * 0.8, 4.0)
        reasons.append("conteúdo com impacto")

    if found_emotion:
        score += min(found_emotion * 0.7, 3.0)
        reasons.append("carga emocional")

    if found_curiosity:
        score += min(found_curiosity * 0.7, 2.0)
        reasons.append("curiosidade")

    return score, reasons


def _closing_score(closing_text: str) -> tuple[float, list[str]]:
    text = closing_text.lower()
    score = 0.0
    reasons = []

    closure_keywords = [
        "por isso",
        "então",
        "ou seja",
        "no fim",
        "a conclusão",
        "é por isso",
        "esse é o ponto",
        "essa é a questão",
    ]

    for kw in closure_keywords:
        if kw in text:
            score += 2.0
            reasons.append("fechamento coerente")
            break

    if len(text.split()) >= 8:
        score += 1.0
        reasons.append("final com substância")

    return score, reasons


def _text_volume_score(full_text: str, mode: str) -> tuple[float, list[str]]:
    word_count = len(full_text.split())
    score = 0.0
    reasons = []

    if mode == "long":
        if 500 <= word_count <= 2500:
            score += 2.5
            reasons.append("volume bom para vídeo longo")
        elif word_count < 250:
            score -= 2.0
            reasons.append("conteúdo curto para vídeo longo")
    else:
        if 40 <= word_count <= 350:
            score += 2.0
            reasons.append("volume bom para corte curto")
        elif word_count < 20:
            score -= 2.0
            reasons.append("texto curto demais")

    return score, reasons


def score_candidates(candidates: list[dict], mode: str = "short") -> list[dict]:
    scored = []

    for candidate in candidates:
        duration = float(candidate.get("duration", 0))
        text = candidate.get("text", "")
        opening_text = candidate.get("opening_text", "")
        closing_text = candidate.get("closing_text", "")

        score = 0.0
        reasons = []

        dur_score, dur_reason = _duration_score(duration, mode)
        score += dur_score
        reasons.append(dur_reason)

        hook_score, hook_reasons = _opening_hook_score(opening_text)
        score += hook_score
        reasons.extend(hook_reasons)

        content_score, content_reasons = _content_strength_score(text)
        score += content_score
        reasons.extend(content_reasons)

        end_score, end_reasons = _closing_score(closing_text)
        score += end_score
        reasons.extend(end_reasons)

        volume_score, volume_reasons = _text_volume_score(text, mode)
        score += volume_score
        reasons.extend(volume_reasons)

        # penalização de monotonia
        if "?" not in opening_text and score < 4:
            score -= 1.0
            reasons.append("gancho inicial fraco")

        scored.append({
            **candidate,
            "score": round(score, 2),
            "reason": ", ".join(dict.fromkeys(reasons)),
        })

    return sorted(scored, key=lambda x: x["score"], reverse=True)