from pathlib import Path
import subprocess

from app.core.config import settings


def extract_audio_from_video(video_path: str, job_id: int) -> str:
    video_file = Path(video_path)
    if not video_file.exists():
        raise FileNotFoundError(f"Vídeo não encontrado: {video_file}")

    downloads_dir = Path(settings.base_data_dir) / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)

    audio_path = downloads_dir / f"job_{job_id}.mp3"

    command = [
        "ffmpeg",
        "-y",
        "-i", str(video_file),
        "-vn",
        "-acodec", "libmp3lame",
        "-ab", "192k",
        str(audio_path)
    ]

    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Erro ao extrair áudio: {e.stderr}") from e

    if not audio_path.exists():
        raise FileNotFoundError(f"Áudio extraído não encontrado: {audio_path}")

    return str(audio_path)