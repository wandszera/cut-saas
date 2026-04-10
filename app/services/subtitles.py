import json
from pathlib import Path
from typing import Any

from app.core.config import settings


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


def _get_layout_config(mode: str) -> dict[str, Any]:
    mode = mode.lower().strip()

    if mode == "long":
        return {
            "play_res_x": 1920,
            "play_res_y": 1080,
            "max_words_per_line": 6,
            "style": {
                "fontname": "Arial",
                "fontsize": 34,
                "primary_colour": "&H00FFFFFF",
                "secondary_colour": "&H00FFFFFF",
                "outline_colour": "&H00000000",
                "back_colour": "&H46000000",
                "bold": -1,
                "italic": 0,
                "outline": 3,
                "shadow": 1,
                "alignment": 2,
                "margin_l": 50,
                "margin_r": 50,
                "margin_v": 40,
            },
        }

    # short = vertical
    return {
        "play_res_x": 1080,
        "play_res_y": 1920,
        "max_words_per_line": 4,
        "style": {
            "fontname": "Arial",
            "fontsize": 54,
            "primary_colour": "&H00FFFFFF",
            "secondary_colour": "&H0000FFFF",
            "outline_colour": "&H00000000",
            "back_colour": "&H50000000",
            "bold": -1,
            "italic": 0,
            "outline": 4,
            "shadow": 1,
            "alignment": 2,
            "margin_l": 40,
            "margin_r": 40,
            "margin_v": 160,
        },
    }


def generate_ass_for_clip(
    transcript_path: str,
    job_id: int,
    clip_index: int,
    clip_start: float,
    clip_end: float,
    mode: str = "short",
) -> str:
    subtitles_dir = Path(settings.base_data_dir) / "subtitles" / f"job_{job_id}"
    subtitles_dir.mkdir(parents=True, exist_ok=True)

    ass_path = subtitles_dir / f"clip_{clip_index + 1}_{mode}.ass"

    selected = _normalize_segments_for_clip(
        transcript_path=transcript_path,
        clip_start=clip_start,
        clip_end=clip_end,
    )

    layout = _get_layout_config(mode)
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
            start_ts = _format_ass_timestamp(item["start"])
            end_ts = _format_ass_timestamp(item["end"])
            text = _wrap_text_ass(
                item["text"],
                max_words_per_line=layout["max_words_per_line"],
            )
            text = text.replace("{", r"\{").replace("}", r"\}")

            f.write(
                f"Dialogue: 0,{start_ts},{end_ts},Default,,0,0,0,,{text}\n"
            )

    return str(ass_path)