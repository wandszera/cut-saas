import json
from pathlib import Path
from typing import Any

from app.core.config import settings


def _format_segment(segment: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": segment.get("id"),
        "start": round(float(segment.get("start", 0.0)), 2),
        "end": round(float(segment.get("end", 0.0)), 2),
        "text": segment.get("text", "").strip(),
    }


def transcribe_audio(
    audio_path: str,
    job_id: int,
    *,
    progress_callback=None,
) -> str:
    """
    Transcreve um arquivo de áudio com Whisper e salva o resultado em JSON.
    """
    if progress_callback:
        progress_callback("Validando arquivo de audio")
    audio_file = Path(audio_path)
    if not audio_file.exists():
        raise FileNotFoundError(f"Áudio não encontrado: {audio_file}")

    transcripts_dir = Path(settings.base_data_dir) / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)

    output_path = transcripts_dir / f"job_{job_id}.json"

    try:
        import whisper

        if progress_callback:
            progress_callback(f"Carregando modelo Whisper ({settings.whisper_model})")
        model = whisper.load_model(settings.whisper_model)

        if progress_callback:
            progress_callback("Executando transcricao do audio")
        result = model.transcribe(
            str(audio_file),
            verbose=False,
            fp16=False
        )

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
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(transcript_data, f, ensure_ascii=False, indent=2)

        if progress_callback:
            progress_callback("Transcricao finalizada")
        return str(output_path)

    except Exception as e:
        raise RuntimeError(f"Erro ao transcrever áudio: {e}") from e
