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
            "min_duration": 300.0,
            "target_duration": 600.0,
            "max_duration": 900.0,
            "window_step_segments": 2,
        }

    return {
        "mode": "short",
        "min_duration": 30.0,
        "target_duration": 90.0,
        "max_duration": 180.0,
        "window_step_segments": 1,
    }


def build_candidate_windows(
    segments: list[dict[str, Any]],
    mode: str = "short",
) -> list[dict[str, Any]]:
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
            seg_end = float(seg["end"])

            group.append(seg)
            last_end = seg_end

            duration = last_end - start

            if duration >= min_duration:
                candidate = _build_candidate(
                    group,
                    mode=mode,
                    previous_segment=segments[i - 1] if i > 0 else None,
                    next_segment=segments[j + 1] if j + 1 < n else None,
                )
                if candidate["duration"] <= max_duration:
                    candidates.append(candidate)

            if duration > max_duration:
                break

    return deduplicate_candidates(candidates)


def split_segments_into_time_chunks(
    segments: list[dict[str, Any]],
    *,
    chunk_duration_seconds: float = 900.0,
    overlap_seconds: float = 45.0,
) -> list[list[dict[str, Any]]]:
    if not segments:
        return []

    chunks: list[list[dict[str, Any]]] = []
    current_chunk: list[dict[str, Any]] = []
    chunk_start = float(segments[0].get("start", 0.0) or 0.0)

    for segment in segments:
        segment_start = float(segment.get("start", 0.0) or 0.0)
        segment_end = float(segment.get("end", segment_start) or segment_start)
        if current_chunk and (segment_end - chunk_start) > chunk_duration_seconds:
            chunks.append(current_chunk)
            overlap_start = max(chunk_start, segment_start - overlap_seconds)
            current_chunk = [
                existing_segment
                for existing_segment in current_chunk
                if float(existing_segment.get("end", existing_segment.get("start", 0.0)) or 0.0) >= overlap_start
            ]
            if not current_chunk:
                current_chunk = [segment]
            elif current_chunk[-1] is not segment:
                current_chunk.append(segment)
            chunk_start = float(current_chunk[0].get("start", segment_start) or segment_start)
            continue

        current_chunk.append(segment)

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def _build_candidate(
    group: list[dict[str, Any]],
    mode: str,
    previous_segment: dict[str, Any] | None = None,
    next_segment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    start = float(group[0]["start"])
    end = float(group[-1]["end"])
    text = " ".join(seg.get("text", "").strip() for seg in group).strip()

    opening_segments = group[: min(3, len(group))]
    middle_segments = group[len(group)//3: (2 * len(group))//3] if len(group) >= 3 else group
    closing_segments = group[-min(3, len(group)) :]

    opening_text = " ".join(seg.get("text", "").strip() for seg in opening_segments).strip()
    middle_text = " ".join(seg.get("text", "").strip() for seg in middle_segments).strip()
    closing_text = " ".join(seg.get("text", "").strip() for seg in closing_segments).strip()
    pause_before = round(start - float(previous_segment["end"]), 2) if previous_segment else 0.0
    pause_after = round(float(next_segment["start"]) - end, 2) if next_segment else 0.0
    starts_clean = not (previous_segment and not str(previous_segment.get("text", "")).strip().endswith((".", "!", "?", ":", ";")))
    ends_clean = bool(str(group[-1].get("text", "")).strip().endswith((".", "!", "?", ":", ";")))

    return {
        "start": round(start, 2),
        "end": round(end, 2),
        "duration": round(end - start, 2),
        "text": text,
        "opening_text": opening_text,
        "middle_text": middle_text,
        "closing_text": closing_text,
        "segments_count": len(group),
        "pause_before": pause_before,
        "pause_after": pause_after,
        "starts_clean": starts_clean,
        "ends_clean": ends_clean,
        "mode": mode,
    }


def deduplicate_candidates(
    candidates: list[dict[str, Any]],
    time_tolerance: float = 8.0,
) -> list[dict[str, Any]]:
    if not candidates:
        return []

    candidates = sorted(candidates, key=lambda x: (x["start"], x["end"]))
    filtered = []

    for cand in candidates:
        is_duplicate = False
        for kept in filtered:
            overlap_start = max(cand["start"], kept["start"])
            overlap_end = min(cand["end"], kept["end"])
            overlap = max(0.0, overlap_end - overlap_start)
            shorter = min(cand["duration"], kept["duration"]) or 1.0
            overlap_ratio = overlap / shorter
            if (
                abs(cand["start"] - kept["start"]) <= time_tolerance
                and abs(cand["end"] - kept["end"]) <= time_tolerance
            ) or overlap_ratio >= 0.9:
                is_duplicate = True
                break

        if not is_duplicate:
            filtered.append(cand)

    return filtered
