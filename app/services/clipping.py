from pathlib import Path
import subprocess

from app.core.config import settings


def _escape_subtitles_path_for_ffmpeg(path: str) -> str:
    p = Path(path).resolve().as_posix()

    if len(p) > 1 and p[1] == ":":
        p = p.replace(":", "\\:")

    p = p.replace("'", r"\'")
    return p


def _build_video_filter(mode: str, subtitles_path: str | None = None) -> str:
    filters = []

    mode = mode.lower().strip()

    if mode == "short":
        # transforma para vertical 9:16
        # crop central + escala
        filters.append("scale=1080:1920:force_original_aspect_ratio=increase")
        filters.append("crop=1080:1920")
    else:
        # garante saída horizontal padrão
        filters.append("scale=1920:1080:force_original_aspect_ratio=decrease")
        filters.append("pad=1920:1080:(ow-iw)/2:(oh-ih)/2")

    if subtitles_path:
        escaped_sub_path = _escape_subtitles_path_for_ffmpeg(subtitles_path)
        filters.append(f"subtitles='{escaped_sub_path}'")

    return ",".join(filters)


def render_clip(
    video_path: str,
    job_id: int,
    clip_index: int,
    start: float,
    end: float,
    mode: str = "short",
    burn_subtitles: bool = False,
    subtitles_path: str | None = None,
) -> str:
    video_file = Path(video_path)
    if not video_file.exists():
        raise FileNotFoundError(f"Vídeo não encontrado: {video_file}")

    clips_dir = Path(settings.base_data_dir) / "clips" / f"job_{job_id}"
    clips_dir.mkdir(parents=True, exist_ok=True)

    suffix = f"_{mode}"
    if burn_subtitles:
        suffix += "_subtitled"

    output_path = clips_dir / f"clip_{clip_index + 1}{suffix}.mp4"
    duration = round(end - start, 2)

    vf = _build_video_filter(
        mode=mode,
        subtitles_path=subtitles_path if burn_subtitles else None,
    )

    command = [
        "ffmpeg",
        "-y",
        "-ss", str(start),
        "-i", str(video_file),
        "-t", str(duration),
        "-vf", vf,
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