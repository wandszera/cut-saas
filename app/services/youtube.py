from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL

from app.core.config import settings


def _build_cookie_options() -> dict:
    opts: dict[str, Any] = {}

    if settings.ytdlp_cookies_file:
        cookies_path = Path(settings.ytdlp_cookies_file)
        if not cookies_path.exists():
            raise FileNotFoundError(f"Arquivo de cookies não encontrado: {cookies_path}")
        opts["cookiefile"] = str(cookies_path)
        return opts

    if settings.ytdlp_cookies_browser:
        browser = settings.ytdlp_cookies_browser.strip()
        if settings.ytdlp_cookies_browser_profile:
            opts["cookiesfrombrowser"] = (
                browser,
                settings.ytdlp_cookies_browser_profile.strip(),
                None,
                None,
            )
        else:
            opts["cookiesfrombrowser"] = (browser,)

    return opts


def _base_opts(output_template: str | None = None) -> dict:
    opts: dict[str, Any] = {
        "noplaylist": True,
        "quiet": not settings.ytdlp_verbose,
        "no_warnings": False,
        "verbose": settings.ytdlp_verbose,
    }

    if output_template:
        opts["outtmpl"] = output_template

    opts.update(_build_cookie_options())
    return opts


def download_youtube_media(url: str, job_id: int) -> dict:
    downloads_dir = Path(settings.base_data_dir) / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)

    video_output = str(downloads_dir / f"job_{job_id}.%(ext)s")

    attempts = [
        "bestvideo+bestaudio/best",
        "best",
    ]

    last_error = None
    info = None

    for fmt in attempts:
        opts = _base_opts(video_output)
        opts["format"] = fmt
        opts["merge_output_format"] = "mp4"

        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            break
        except Exception as e:
            last_error = e

    if info is None:
        raise RuntimeError(f"Erro ao baixar vídeo do YouTube: {last_error}")

    title = info.get("title", f"job_{job_id}")
    video_id = info.get("id", "")

    video_path = downloads_dir / f"job_{job_id}.mp4"
    if not video_path.exists():
        # fallback: tenta encontrar qualquer arquivo compatível
        possible_files = list(downloads_dir.glob(f"job_{job_id}.*"))
        video_candidates = [p for p in possible_files if p.suffix.lower() in [".mp4", ".mkv", ".webm"]]
        if not video_candidates:
            raise FileNotFoundError(f"Vídeo baixado não encontrado para job {job_id}")
        video_path = video_candidates[0]

    return {
        "video_path": str(video_path),
        "title": title,
        "video_id": video_id,
    }