from pathlib import Path
import subprocess

from app.services.storage import get_storage, normalize_storage_key


def extract_audio_from_video(video_path: str, job_id: int) -> str:
    video_file = Path(video_path)
    if not video_file.exists():
        raise FileNotFoundError(f"Vídeo não encontrado: {video_file}")

    audio_path = get_storage().path_for(normalize_storage_key("downloads", f"job_{job_id}.mp3"))

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

    get_storage().sync_path(audio_path)
    return str(audio_path)
