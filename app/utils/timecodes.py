def parse_timecode_to_seconds(value: float | int | str) -> float:
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds < 0:
            raise ValueError("Tempo não pode ser negativo")
        return round(seconds, 2)

    raw_value = str(value or "").strip()
    if not raw_value:
        raise ValueError("Tempo não informado")

    normalized = raw_value.replace(",", ".")
    if ":" not in normalized:
        try:
            seconds = float(normalized)
        except ValueError as exc:
            raise ValueError("Tempo inválido. Use segundos ou hh:mm:ss.") from exc
        if seconds < 0:
            raise ValueError("Tempo não pode ser negativo")
        return round(seconds, 2)

    parts = normalized.split(":")
    if len(parts) not in {2, 3}:
        raise ValueError("Tempo inválido. Use mm:ss ou hh:mm:ss.")

    try:
        numeric_parts = [float(part) for part in parts]
    except ValueError as exc:
        raise ValueError("Tempo inválido. Use números em mm:ss ou hh:mm:ss.") from exc

    if any(part < 0 for part in numeric_parts):
        raise ValueError("Tempo não pode ser negativo")

    if len(numeric_parts) == 2:
        minutes, seconds = numeric_parts
        hours = 0.0
    else:
        hours, minutes, seconds = numeric_parts

    if minutes >= 60 or seconds >= 60:
        raise ValueError("Minutos e segundos devem ser menores que 60.")

    total_seconds = (hours * 3600) + (minutes * 60) + seconds
    return round(total_seconds, 2)
