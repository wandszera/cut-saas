import json
import time
from pathlib import Path
from threading import Lock, Thread
from typing import Any

from app.core.config import settings
from app.services.storage import get_storage, normalize_storage_key

_MODEL_CACHE: dict[str, Any] = {}
_MODEL_CACHE_LOCK = Lock()
_DEVICE_CAPABILITY_CACHE: bool | None = None
_DEVICE_CAPABILITY_LOCK = Lock()
_PROGRESS_HEARTBEAT_INTERVAL_SECONDS = 15.0


def _format_segment(segment: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": segment.get("id"),
        "start": round(float(segment.get("start", 0.0)), 2),
        "end": round(float(segment.get("end", 0.0)), 2),
        "text": segment.get("text", "").strip(),
    }


def _get_whisper_model(*, progress_callback=None) -> Any:
    model_name = settings.whisper_model
    cached_model = _MODEL_CACHE.get(model_name)
    if cached_model is not None:
        if progress_callback:
            progress_callback(f"Reutilizando modelo Whisper ({model_name})")
        return cached_model

    with _MODEL_CACHE_LOCK:
        cached_model = _MODEL_CACHE.get(model_name)
        if cached_model is not None:
            if progress_callback:
                progress_callback(f"Reutilizando modelo Whisper ({model_name})")
            return cached_model

        import whisper

        if progress_callback:
            progress_callback(f"Carregando modelo Whisper ({model_name})")
        model = whisper.load_model(model_name)
        _MODEL_CACHE[model_name] = model
        return model


def _resolve_transcription_provider() -> str:
    configured_provider = settings.transcription_provider
    if configured_provider != "auto":
        return configured_provider

    try:
        import faster_whisper  # noqa: F401

        return "faster_whisper"
    except Exception:
        return "openai_whisper"


def _get_faster_whisper_model(*, progress_callback=None) -> Any:
    model_name = f"faster_whisper::{settings.whisper_model}"
    cached_model = _MODEL_CACHE.get(model_name)
    if cached_model is not None:
        if progress_callback:
            progress_callback(f"Reutilizando modelo Faster Whisper ({settings.whisper_model})")
        return cached_model

    with _MODEL_CACHE_LOCK:
        cached_model = _MODEL_CACHE.get(model_name)
        if cached_model is not None:
            if progress_callback:
                progress_callback(f"Reutilizando modelo Faster Whisper ({settings.whisper_model})")
            return cached_model

        from faster_whisper import WhisperModel

        compute_type = "float16" if _resolve_fp16_mode() else "float32"
        device = "cuda" if compute_type == "float16" else "cpu"

        if progress_callback:
            progress_callback(
                f"Carregando Faster Whisper ({settings.whisper_model}, {device}, {compute_type})"
            )
        model = WhisperModel(
            settings.whisper_model,
            device=device,
            compute_type=compute_type,
        )
        _MODEL_CACHE[model_name] = model
        return model


def _detect_cuda_fp16_support() -> bool:
    global _DEVICE_CAPABILITY_CACHE

    if _DEVICE_CAPABILITY_CACHE is not None:
        return _DEVICE_CAPABILITY_CACHE

    with _DEVICE_CAPABILITY_LOCK:
        if _DEVICE_CAPABILITY_CACHE is not None:
            return _DEVICE_CAPABILITY_CACHE

        try:
            import torch

            supported = bool(torch.cuda.is_available())
        except Exception:
            supported = False

        _DEVICE_CAPABILITY_CACHE = supported
        return supported


def _resolve_fp16_mode(*, progress_callback=None) -> bool:
    precision_mode = settings.whisper_precision
    if precision_mode == "fp16":
        if progress_callback:
            progress_callback("Usando transcricao Whisper em fp16")
        return True
    if precision_mode == "fp32":
        if progress_callback:
            progress_callback("Usando transcricao Whisper em fp32")
        return False

    use_fp16 = _detect_cuda_fp16_support()
    if progress_callback:
        if use_fp16:
            progress_callback("GPU detectada, usando transcricao Whisper em fp16")
        else:
            progress_callback("GPU nao detectada, usando transcricao Whisper em fp32")
    return use_fp16


def _transcribe_with_openai_whisper(model: Any, audio_file: Path, *, use_fp16: bool) -> dict[str, Any]:
    return model.transcribe(
        str(audio_file),
        verbose=False,
        fp16=use_fp16,
    )


def _transcribe_with_faster_whisper(model: Any, audio_file: Path) -> dict[str, Any]:
    segments_iter, info = model.transcribe(str(audio_file), beam_size=5)
    segments = []
    full_text_parts: list[str] = []
    for index, segment in enumerate(segments_iter):
        text = segment.text.strip()
        segments.append(
            {
                "id": index,
                "start": round(float(segment.start), 2),
                "end": round(float(segment.end), 2),
                "text": text,
            }
        )
        if text:
            full_text_parts.append(text)

    return {
        "language": getattr(info, "language", None),
        "text": " ".join(full_text_parts).strip(),
        "segments": segments,
    }


def _run_transcription_job(
    audio_file: Path,
    output_path: Path,
    job_id: int,
    *,
    progress_callback=None,
) -> str:
    provider = _resolve_transcription_provider()
    use_fp16 = _resolve_fp16_mode(progress_callback=progress_callback)

    if progress_callback:
        progress_callback(f"Provider de transcricao selecionado: {provider}")

    if provider == "faster_whisper":
        model = _get_faster_whisper_model(progress_callback=progress_callback)
    else:
        model = _get_whisper_model(progress_callback=progress_callback)

    if progress_callback:
        progress_callback("Executando transcricao do audio")
    if provider == "faster_whisper":
        result = _transcribe_with_faster_whisper(model, audio_file)
    else:
        result = _transcribe_with_openai_whisper(model, audio_file, use_fp16=use_fp16)

    if progress_callback:
        progress_callback("Processando segmentos e consolidando texto")
    segments = [_format_segment(seg) for seg in result.get("segments", [])]

    transcript_data = {
        "job_id": job_id,
        "audio_path": str(audio_file),
        "language": result.get("language"),
        "text": result.get("text", "").strip(),
        "segments_count": len(segments),
        "segments": segments,
    }

    if progress_callback:
        progress_callback("Salvando transcricao em JSON")
    with open(output_path, "w", encoding="utf-8") as file_handle:
        json.dump(transcript_data, file_handle, ensure_ascii=False, indent=2)

    get_storage().sync_path(output_path)
    if progress_callback:
        progress_callback("Transcricao finalizada")
    return str(output_path)


def _transcribe_with_heartbeat(
    audio_file: Path,
    output_path: Path,
    job_id: int,
    *,
    progress_callback=None,
) -> str:
    if progress_callback is None:
        return _run_transcription_job(audio_file, output_path, job_id)

    shared_state: dict[str, list[str] | str | None] = {"message": None, "pending_messages": []}
    result_holder: dict[str, str] = {}
    error_holder: dict[str, Exception] = {}

    def _thread_progress(message: str) -> None:
        shared_state["message"] = message
        pending_messages = shared_state["pending_messages"]
        assert isinstance(pending_messages, list)
        pending_messages.append(message)

    def _worker() -> None:
        try:
            result_holder["path"] = _run_transcription_job(
                audio_file,
                output_path,
                job_id,
                progress_callback=_thread_progress,
            )
        except Exception as exc:  # pragma: no cover
            error_holder["error"] = exc

    worker = Thread(target=_worker, daemon=True)
    worker.start()

    last_message = None
    last_emitted_at = 0.0

    while worker.is_alive():
        pending_messages = shared_state["pending_messages"]
        assert isinstance(pending_messages, list)
        while pending_messages:
            message = pending_messages.pop(0)
            progress_callback(message)
            last_message = message
            last_emitted_at = time.monotonic()

        message = shared_state["message"]
        now = time.monotonic()
        if message and (now - last_emitted_at) >= _PROGRESS_HEARTBEAT_INTERVAL_SECONDS:
            progress_callback(message)
            last_emitted_at = now
        worker.join(timeout=max(0.05, min(1.0, _PROGRESS_HEARTBEAT_INTERVAL_SECONDS / 2)))

    pending_messages = shared_state["pending_messages"]
    assert isinstance(pending_messages, list)
    while pending_messages:
        message = pending_messages.pop(0)
        progress_callback(message)
        last_message = message

    final_message = shared_state["message"]
    if final_message and final_message != last_message:
        progress_callback(final_message)

    error = error_holder.get("error")
    if error is not None:
        raise error

    return result_holder["path"]


def transcribe_audio(
    audio_path: str,
    job_id: int,
    *,
    progress_callback=None,
) -> str:
    """
    Transcreve um arquivo de audio com Whisper e salva o resultado em JSON.
    """
    if progress_callback:
        progress_callback("Validando arquivo de audio")
    audio_file = Path(audio_path)
    if not audio_file.exists():
        raise FileNotFoundError(f"Audio nao encontrado: {audio_file}")

    output_path = get_storage().path_for(normalize_storage_key("transcripts", f"job_{job_id}.json"))

    try:
        return _transcribe_with_heartbeat(
            audio_file,
            output_path,
            job_id,
            progress_callback=progress_callback,
        )
    except Exception as exc:
        raise RuntimeError(f"Erro ao transcrever audio: {exc}") from exc
