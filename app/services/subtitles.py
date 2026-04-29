import json
import re
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.services.render_presets import resolve_render_preset
from app.services.storage import get_storage, normalize_storage_key


def _format_ass_timestamp(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centis = int(round((seconds - int(seconds)) * 100))

    if centis == 100:
        secs += 1
        centis = 0
    if secs == 60:
        minutes += 1
        secs = 0
    if minutes == 60:
        hours += 1
        minutes = 0

    return f"{hours}:{minutes:02}:{secs:02}.{centis:02}"


def load_transcript_data(transcript_path: str) -> dict[str, Any]:
    with open(transcript_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _normalize_segments_for_clip(
    transcript_path: str,
    clip_start: float,
    clip_end: float,
) -> list[dict[str, Any]]:
    data = load_transcript_data(transcript_path)
    segments = data.get("segments", [])
    selected = []

    for seg in segments:
        seg_start = float(seg.get("start", 0.0))
        seg_end = float(seg.get("end", 0.0))
        seg_text = seg.get("text", "").strip()

        if seg_end <= clip_start or seg_start >= clip_end:
            continue

        local_start = max(seg_start, clip_start) - clip_start
        local_end = min(seg_end, clip_end) - clip_start

        if local_end <= local_start:
            continue

        if seg_text:
            selected.append(
                {
                    "start": local_start,
                    "end": local_end,
                    "text": seg_text,
                }
            )

    return selected


def _wrap_text_ass(text: str, max_words_per_line: int) -> str:
    words = text.split()
    if not words:
        return ""

    lines = []
    current = []

    for word in words:
        current.append(word)
        if len(current) >= max_words_per_line:
            lines.append(" ".join(current))
            current = []

    if current:
        lines.append(" ".join(current))

    return r"\N".join(lines)


def _normalize_text(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    return cleaned


def _chunk_words_balanced(
    words: list[str],
    *,
    max_words_per_line: int,
    max_chars_per_line: int,
) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []

    for word in words:
        proposed = current + [word]
        proposed_text = " ".join(proposed)
        if current and (
            len(proposed) > max_words_per_line or len(proposed_text) > max_chars_per_line
        ):
            chunks.append(current)
            current = [word]
            continue
        current = proposed

    if current:
        chunks.append(current)

    return chunks


def _rebalance_last_chunks(chunks: list[list[str]], max_chars_per_line: int) -> list[list[str]]:
    if len(chunks) < 2:
        return chunks

    previous = chunks[-2][:]
    last = chunks[-1][:]
    while len(" ".join(last)) < max(8, max_chars_per_line // 2) and len(previous) > 1:
        last.insert(0, previous.pop())
        if len(" ".join(previous)) > max_chars_per_line or len(" ".join(last)) > max_chars_per_line:
            previous.append(last.pop(0))
            break

    chunks[-2] = previous
    chunks[-1] = last
    return chunks


def _split_segment_text(
    text: str,
    *,
    max_words_per_line: int,
    max_chars_per_line: int,
    max_lines: int,
) -> list[str]:
    cleaned = _normalize_text(text)
    if not cleaned:
        return []

    phrase_parts = re.split(r"(?<=[,.;:!?])\s+", cleaned)
    captions: list[str] = []
    current_words: list[str] = []

    def flush_words(buffer: list[str]) -> None:
        if not buffer:
            return
        chunks = _chunk_words_balanced(
            buffer,
            max_words_per_line=max_words_per_line,
            max_chars_per_line=max_chars_per_line,
        )
        chunks = _rebalance_last_chunks(chunks, max_chars_per_line)

        for index in range(0, len(chunks), max_lines):
            lines = [" ".join(chunk) for chunk in chunks[index:index + max_lines]]
            captions.append(r"\N".join(lines))

    for part in phrase_parts:
        part_words = part.split()
        if not part_words:
            continue
        proposed = current_words + part_words
        proposed_chunks = _chunk_words_balanced(
            proposed,
            max_words_per_line=max_words_per_line,
            max_chars_per_line=max_chars_per_line,
        )
        if current_words and len(proposed_chunks) > max_lines:
            flush_words(current_words)
            current_words = part_words
        else:
            current_words = proposed

    flush_words(current_words)
    return captions or [cleaned]


def _escape_ass_text(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def _build_karaoke_text(text: str, duration_seconds: float) -> str:
    parts = text.split(r"\N")
    visible_words = []
    for part in parts:
        visible_words.extend(part.split())

    if not visible_words:
        return _escape_ass_text(text)

    total_centis = max(1, int(round(duration_seconds * 100)))
    base = total_centis // len(visible_words)
    remainder = total_centis % len(visible_words)

    durations = [base] * len(visible_words)
    for index in range(remainder):
        durations[index] += 1

    duration_index = 0
    karaoke_lines: list[str] = []
    for part in parts:
        line_words = part.split()
        encoded_words = []
        for word in line_words:
            current_duration = durations[duration_index]
            duration_index += 1
            encoded_words.append(f"{{\\k{current_duration}}}{_escape_ass_text(word)}")
        karaoke_lines.append(" ".join(encoded_words))

    return r"\N".join(karaoke_lines)


def _get_layout_config(mode: str, render_preset: str | None = None) -> dict[str, Any]:
    mode = mode.lower().strip()
    preset_name, preset = resolve_render_preset(render_preset)

    if mode == "long":
        style = preset["subtitles"]["long"]
        return {
            "play_res_x": 1920,
            "play_res_y": 1080,
            "max_words_per_line": style["max_words_per_line"],
            "max_chars_per_line": style.get("max_chars_per_line", 28),
            "max_lines": style.get("max_lines", 2),
            "karaoke_enabled": style.get("karaoke_enabled", False),
            "style": style,
            "preset_name": preset_name,
        }

    style = preset["subtitles"]["short"]
    return {
        "play_res_x": 1080,
        "play_res_y": 1920,
        "max_words_per_line": style["max_words_per_line"],
        "max_chars_per_line": style.get("max_chars_per_line", 18),
        "max_lines": style.get("max_lines", 2),
        "karaoke_enabled": style.get("karaoke_enabled", False),
        "style": style,
        "preset_name": preset_name,
    }


def generate_ass_for_clip(
    transcript_path: str,
    job_id: int,
    clip_index: int,
    clip_start: float,
    clip_end: float,
    mode: str = "short",
    render_preset: str | None = None,
) -> str:
    ass_path = get_storage().path_for(
        normalize_storage_key("subtitles", f"job_{job_id}", f"clip_{clip_index + 1}_{mode}.ass")
    )

    selected = _normalize_segments_for_clip(
        transcript_path=transcript_path,
        clip_start=clip_start,
        clip_end=clip_end,
    )

    layout = _get_layout_config(mode, render_preset)
    style = layout["style"]

    header = f"""[Script Info]
Title: Styled Subtitles
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.601
PlayResX: {layout["play_res_x"]}
PlayResY: {layout["play_res_y"]}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{style["fontname"]},{style["fontsize"]},{style["primary_colour"]},{style["secondary_colour"]},{style["outline_colour"]},{style["back_colour"]},{style["bold"]},{style["italic"]},0,0,100,100,0,0,1,{style["outline"]},{style["shadow"]},{style["alignment"]},{style["margin_l"]},{style["margin_r"]},{style["margin_v"]},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header)

        for item in selected:
            caption_blocks = _split_segment_text(
                item["text"],
                max_words_per_line=layout["max_words_per_line"],
                max_chars_per_line=layout["max_chars_per_line"],
                max_lines=layout["max_lines"],
            )
            if not caption_blocks:
                continue

            segment_duration = max(item["end"] - item["start"], 0.01)
            block_duration = segment_duration / len(caption_blocks)

            for index, text in enumerate(caption_blocks):
                block_start = item["start"] + (index * block_duration)
                block_end = item["end"] if index == len(caption_blocks) - 1 else block_start + block_duration
                start_ts = _format_ass_timestamp(block_start)
                end_ts = _format_ass_timestamp(block_end)
                safe_text = (
                    _build_karaoke_text(text, block_end - block_start)
                    if layout["karaoke_enabled"]
                    else _escape_ass_text(text)
                )
                f.write(
                    f"Dialogue: 0,{start_ts},{end_ts},Default,,0,0,0,,{safe_text}\n"
                )

    get_storage().sync_path(ass_path)
    return str(ass_path)
