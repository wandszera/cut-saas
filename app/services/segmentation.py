import json
from typing import Any


def load_transcript(transcript_path: str) -> dict[str, Any]:
    with open(transcript_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_segments(transcript_path: str) -> list[dict[str, Any]]:
    data = load_transcript(transcript_path)
    return data.get("segments", [])


def get_mode_config(mode: str) -> dict[str, Any]:
    mode = mode.lower().strip()

    if mode == "long":
        return {
            "mode": "long",
            "min_duration": 300.0,   # 5 min
            "target_duration": 600.0, # 10 min
            "max_duration": 900.0,   # 15 min
            "window_step_segments": 2,
        }

    # default = short
    return {
        "mode": "short",
        "min_duration": 30.0,
        "target_duration": 120.0,
        "max_duration": 180.0,
        "window_step_segments": 1,
    }


def build_candidate_windows(
    segments: list[dict[str, Any]],
    mode: str = "short",
) -> list[dict[str, Any]]:
    """
    Gera múltiplos candidatos por janelas de tempo.
    Em vez de apenas agrupar linearmente, testa várias janelas com tamanhos adequados ao modo.
    """
    if not segments:
        return []

    cfg = get_mode_config(mode)
    min_duration = cfg["min_duration"]
    max_duration = cfg["max_duration"]
    step = cfg["window_step_segments"]

    candidates = []
    n = len(segments)

    for i in range(0, n, step):
        start = float(segments[i]["start"])
        group = []
        last_end = start

        for j in range(i, n):
            seg = segments[j]
            seg_start = float(seg["start"])
            seg_end = float(seg["end"])

            if not group:
                group.append(seg)
                last_end = seg_end
                continue

            group.append(seg)
            last_end = seg_end

            duration = last_end - start

            if duration >= min_duration:
                candidate = _build_candidate(group, mode=mode)
                if candidate["duration"] <= max_duration:
                    candidates.append(candidate)

            if duration > max_duration:
                break

    # remove duplicados muito parecidos
    return deduplicate_candidates(candidates)


def _build_candidate(group: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    start = float(group[0]["start"])
    end = float(group[-1]["end"])
    text = " ".join(seg.get("text", "").strip() for seg in group).strip()

    opening_segments = group[: min(3, len(group))]
    closing_segments = group[-min(3, len(group)) :]

    opening_text = " ".join(seg.get("text", "").strip() for seg in opening_segments).strip()
    closing_text = " ".join(seg.get("text", "").strip() for seg in closing_segments).strip()

    return {
        "start": round(start, 2),
        "end": round(end, 2),
        "duration": round(end - start, 2),
        "text": text,
        "opening_text": opening_text,
        "closing_text": closing_text,
        "segments_count": len(group),
        "mode": mode,
    }


def deduplicate_candidates(
    candidates: list[dict[str, Any]],
    time_tolerance: float = 8.0,
) -> list[dict[str, Any]]:
    """
    Remove candidatos quase iguais.
    """
    if not candidates:
        return []

    candidates = sorted(candidates, key=lambda x: (x["start"], x["end"]))
    filtered = []

    for cand in candidates:
        is_duplicate = False
        for kept in filtered:
            if (
                abs(cand["start"] - kept["start"]) <= time_tolerance
                and abs(cand["end"] - kept["end"]) <= time_tolerance
            ):
                is_duplicate = True
                break

        if not is_duplicate:
            filtered.append(cand)

    return filtered