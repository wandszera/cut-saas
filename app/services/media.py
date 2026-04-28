from pathlib import Path
import subprocess


def probe_video_duration_seconds(video_path: str | Path) -> float:
    video_file = Path(video_path)
    if not video_file.exists() or not video_file.is_file():
        raise FileNotFoundError(f"Video nao encontrado: {video_file}")

    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_file),
    ]

    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Erro ao ler duracao do video: {exc.stderr}") from exc

    raw_duration = (result.stdout or "").strip()
    try:
        duration_seconds = float(raw_duration)
    except ValueError as exc:
        raise ValueError("Duracao do video invalida") from exc

    if duration_seconds <= 0:
        raise ValueError("Duracao do video invalida")
    return duration_seconds
