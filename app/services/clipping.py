from pathlib import Path
import subprocess

from app.core.config import settings
from app.services.render_presets import resolve_render_preset


def _escape_subtitles_path_for_ffmpeg(path: str) -> str:
    p = Path(path).resolve().as_posix()

    if len(p) > 1 and p[1] == ":":
        p = p.replace(":", "\\:")

    p = p.replace("'", r"\'")
    return p


def _build_short_filter(
    subtitles_path: str | None = None,
    blur_strength: str = "20:2",
) -> str:
    """
    Short vertical com fundo blur + vídeo principal centralizado.
    """
    filter_parts = [
        # fundo vertical desfocado
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        f"boxblur={blur_strength}[bg]",

        # vídeo principal cabendo inteiro dentro do vertical
        "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg]",

        # centraliza foreground sobre background
        "[bg][fg]overlay=(W-w)/2:(H-h)/2"
    ]

    if subtitles_path:
        escaped_sub_path = _escape_subtitles_path_for_ffmpeg(subtitles_path)
        filter_parts[-1] += f",subtitles='{escaped_sub_path}'"

    return ";".join(filter_parts)


def _build_long_filter(subtitles_path: str | None = None) -> str:
    """
    Long horizontal padrão.
    """
    vf = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2"

    if subtitles_path:
        escaped_sub_path = _escape_subtitles_path_for_ffmpeg(subtitles_path)
        vf += f",subtitles='{escaped_sub_path}'"

    return vf


def render_clip(
    video_path: str,
    job_id: int,
    clip_index: int,
    start: float,
    end: float,
    mode: str = "short",
    burn_subtitles: bool = False,
    subtitles_path: str | None = None,
    render_preset: str | None = None,
) -> str:
    video_file = Path(video_path)
    if not video_file.exists():
        raise FileNotFoundError(f"Vídeo não encontrado: {video_file}")

    clips_dir = Path(settings.base_data_dir) / "clips" / f"job_{job_id}"
    clips_dir.mkdir(parents=True, exist_ok=True)
    preset_name, preset = resolve_render_preset(render_preset)

    suffix = f"_{mode}"
    if burn_subtitles:
        suffix += "_subtitled"
    suffix += f"_{preset_name}"

    output_path = clips_dir / f"clip_{clip_index + 1}{suffix}.mp4"
    duration = round(end - start, 2)

    command = [
        "ffmpeg",
        "-y",
        "-ss", str(start),
        "-i", str(video_file),
        "-t", str(duration),
    ]

    if mode == "short":
        filter_complex = _build_short_filter(
            subtitles_path=subtitles_path if burn_subtitles else None,
            blur_strength=preset["video"]["short"].get("blur_strength", "20:2"),
        )
        command += [
            "-filter_complex", filter_complex,
            "-map", "0:a?"
        ]
    else:
        vf = _build_long_filter(
            subtitles_path=subtitles_path if burn_subtitles else None
        )
        command += [
            "-vf", vf
        ]

    command += [
        "-c:v", "libx264",
        "-c:a", "aac",
        "-movflags", "+faststart",
        str(output_path)
    ]

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Erro ao renderizar clip: {e.stderr}") from e

    if not output_path.exists():
        raise FileNotFoundError(f"Clip não foi gerado: {output_path}")

    return str(output_path)
