import logging
from pathlib import Path
from typing import Any
import os

from yt_dlp import YoutubeDL

from app.core.config import settings
from app.services.storage import get_storage, normalize_storage_key
from app.utils.runtime_env import build_runtime_env, detect_node

logger = logging.getLogger(__name__)


def _build_cookie_options() -> dict:
    opts: dict[str, Any] = {}

    if settings.ytdlp_cookies_file:
        cookies_path = Path(settings.ytdlp_cookies_file)
        if not cookies_path.exists():
            logger.warning("Arquivo de cookies não encontrado: %s — continuando sem cookies", cookies_path)
        else:
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


def _prepare_process_environment() -> dict[str, str]:
    env = build_runtime_env()

    # garante que o processo atual também enxergue o PATH novo
    os.environ["PATH"] = env.get("PATH", os.environ.get("PATH", ""))

    return env


def _build_js_runtime_options() -> dict[str, Any]:
    info = detect_node()
    if not info.get("available"):
        return {}

    node_bin = str(info.get("node_bin") or settings.node_bin or "node").strip()
    return {
        "js_runtimes": {
            "node": {"path": node_bin},
        }
    }


def _check_node_or_raise() -> None:
    info = detect_node()
    if not info["available"]:
        raise RuntimeError(
            "Node.js não está disponível para o backend. "
            f"node_bin={info.get('node_bin')} "
            f"resolved_path={info.get('resolved_path')} "
            f"error={info.get('error')}"
        )


def fetch_youtube_metadata(url: str) -> dict:
    _prepare_process_environment()
    _check_node_or_raise()

    opts = _base_opts()
    opts["skip_download"] = True
    opts.update(_build_js_runtime_options())

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        if "cookiefile" in opts or "cookiesfrombrowser" in opts:
            logger.warning("Falha ao consultar metadados com cookies: %s. Tentando sem cookies...", exc)
            opts_no_cookies = opts.copy()
            opts_no_cookies.pop("cookiefile", None)
            opts_no_cookies.pop("cookiesfrombrowser", None)
            try:
                with YoutubeDL(opts_no_cookies) as ydl:
                    info = ydl.extract_info(url, download=False)
            except Exception as exc_inner:
                raise RuntimeError(f"Erro ao consultar metadados do YouTube (mesmo sem cookies): {exc_inner}") from exc_inner
        else:
            raise RuntimeError(f"Erro ao consultar metadados do YouTube: {exc}") from exc

    return {
        "title": info.get("title"),
        "video_id": info.get("id"),
        "duration_seconds": float(info.get("duration") or 0.0),
    }


def download_youtube_media(url: str, job_id: int) -> dict:
    storage = get_storage()
    downloads_dir = storage.ensure_prefix("downloads")

    video_output = str(storage.path_for(normalize_storage_key("downloads", f"job_{job_id}.%(ext)s")))

    # prepara PATH do processo atual
    _prepare_process_environment()

    # valida Node antes do yt-dlp
    _check_node_or_raise()

    attempts = [
        "bestvideo*+bestaudio/best",
        "bestvideo+bestaudio/best",
        "best[ext=mp4]/best",
        "best",
    ]

    last_error = None
    info = None

    for fmt in attempts:
        opts = _base_opts(video_output)
        opts["format"] = fmt
        opts["merge_output_format"] = "mp4"
        opts.update(_build_js_runtime_options())

        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            break
        except Exception as e:
            if "cookiefile" in opts or "cookiesfrombrowser" in opts:
                logger.warning("Falha no download com cookies: %s. Tentando sem cookies...", e)
                opts_no_cookies = opts.copy()
                opts_no_cookies.pop("cookiefile", None)
                opts_no_cookies.pop("cookiesfrombrowser", None)
                try:
                    with YoutubeDL(opts_no_cookies) as ydl:
                        info = ydl.extract_info(url, download=True)
                    break
                except Exception as e_inner:
                    last_error = e_inner
            else:
                last_error = e

    if info is None:
        raise RuntimeError(f"Erro ao baixar vídeo do YouTube: {last_error}")

    title = info.get("title", f"job_{job_id}")
    video_id = info.get("id", "")

    video_path = downloads_dir / f"job_{job_id}.mp4"
    if not video_path.exists():
        possible_files = list(downloads_dir.glob(f"job_{job_id}.*"))
        video_candidates = [
            p for p in possible_files
            if p.suffix.lower() in [".mp4", ".mkv", ".webm"]
        ]
        if not video_candidates:
            raise FileNotFoundError(f"Vídeo baixado não encontrado para job {job_id}")
        video_path = video_candidates[0]

    storage.sync_path(video_path)
    return {
        "video_path": str(video_path),
        "title": title,
        "video_id": video_id,
    }


def download_youtube_subtitle(
    url: str,
    job_id: int,
    *,
    preferred_languages: list[str] | None = None,
) -> str | None:
    """
    Tenta baixar a legenda de um vídeo do YouTube usando yt-dlp.

    Tenta na ordem:
    1. Legendas manuais nos idiomas de `preferred_languages`
    2. Legendas automáticas nos idiomas de `preferred_languages`
    3. Fallback: qualquer legenda manual disponível
    4. Fallback: qualquer legenda automática disponível

    Retorna o path do arquivo de legenda (.vtt) ou None se não houver legenda.
    """
    from app.core.config import settings

    if preferred_languages is None:
        preferred_languages = [
            lang.strip()
            for lang in (settings.youtube_subtitle_languages or "pt,pt-BR,en").split(",")
            if lang.strip()
        ]

    storage = get_storage()
    subtitles_dir = storage.ensure_prefix("subtitles")
    output_template = str(
        storage.path_for(normalize_storage_key("subtitles", f"job_{job_id}.%(ext)s"))
    )

    _prepare_process_environment()

    # yt-dlp pode não exigir Node para legendas, mas mantemos a consistência
    try:
        _check_node_or_raise()
    except RuntimeError as exc:
        logger.warning("Node.js indisponível para download de legenda: %s", exc)

    lang_str = ",".join(preferred_languages)

    # Tentativas: (writesubtitles, writeautomaticsub) × (idiomas específicos, qualquer)
    attempts = [
        # 1. Legenda manual, idiomas preferidos
        {"writesubtitles": True, "writeautomaticsub": False, "subtitleslangs": preferred_languages},
        # 2. Legenda automática, idiomas preferidos
        {"writesubtitles": False, "writeautomaticsub": True, "subtitleslangs": preferred_languages},
        # 3. Qualquer legenda manual
        {"writesubtitles": True, "writeautomaticsub": False, "subtitleslangs": ["all"]},
        # 4. Qualquer legenda automática
        {"writesubtitles": False, "writeautomaticsub": True, "subtitleslangs": ["all"]},
    ]

    for attempt_opts in attempts:
        opts = _base_opts(output_template)
        opts["skip_download"] = True
        opts["subtitlesformat"] = "vtt/srt/best"
        opts.update(attempt_opts)
        opts.update(_build_js_runtime_options())

        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)

            # Procura o arquivo de legenda gerado
            subtitle_files = list(subtitles_dir.glob(f"job_{job_id}.*"))
            vtt_files = [p for p in subtitle_files if p.suffix.lower() in {".vtt", ".srt"}]
            if vtt_files:
                subtitle_path = vtt_files[0]
                logger.info(
                    "Legenda baixada com sucesso: job_id=%s path=%s",
                    job_id,
                    subtitle_path,
                )
                return str(subtitle_path)
        except Exception as exc:
            logger.debug(
                "Tentativa de download de legenda falhou (opts=%s): %s",
                attempt_opts,
                exc,
            )
            continue

    logger.info("Nenhuma legenda disponível para o vídeo: job_id=%s url=%s", job_id, url)
    return None

