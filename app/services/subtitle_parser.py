"""
Converte arquivos de legenda (VTT/SRT) para o formato JSON de transcrição interno do projeto.

O formato alvo é compatível com o que `transcription.py` gera via Whisper:
{
    "job_id": int,
    "source": "youtube_subtitle",
    "language": str | None,
    "text": str,
    "segments_count": int,
    "segments": [{"id": int, "start": float, "end": float, "text": str}]
}
"""
import json
import logging
import re
from pathlib import Path
from typing import Any

from app.services.storage import get_storage, normalize_storage_key

logger = logging.getLogger(__name__)

# Tags HTML do VTT automático do YouTube: <c>, <00:00:01.000>, <b>, etc.
_HTML_TAG_RE = re.compile(r"<[^>]+>")
# Timestamp VTT: 00:00:01.000 ou 0:00:01.000
_VTT_TIMESTAMP_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{3})"
)
# Linha de cue VTT: "00:00:01.000 --> 00:00:03.000 ..."
_VTT_CUE_RE = re.compile(
    r"(\d{1,2}:\d{2}:\d{2}[.,]\d{3})\s+-->\s+(\d{1,2}:\d{2}:\d{2}[.,]\d{3})"
)
# Timestamp SRT: 00:00:01,000 --> 00:00:03,000
_SRT_CUE_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2},\d{3})"
)


def _parse_timestamp(ts: str) -> float:
    """Converte timestamp VTT/SRT ('HH:MM:SS.mmm' ou 'HH:MM:SS,mmm') em segundos."""
    ts = ts.replace(",", ".")
    parts = ts.split(":")
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    if len(parts) == 2:
        m, s = parts
        return int(m) * 60 + float(s)
    return float(ts)


def _clean_text(raw: str) -> str:
    """Remove tags HTML e espaços extras do texto da legenda."""
    cleaned = _HTML_TAG_RE.sub("", raw)
    # normaliza espaços múltiplos e quebras de linha internas
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _detect_language_from_vtt(content: str) -> str | None:
    """Tenta detectar o idioma a partir do cabeçalho do VTT (Language: pt-BR)."""
    for line in content.splitlines()[:20]:
        match = re.match(r"Language:\s*(.+)", line, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def _parse_vtt(content: str) -> tuple[list[dict[str, Any]], str | None]:
    """Parse de arquivo WebVTT. Retorna (segmentos, idioma_detectado)."""
    language = _detect_language_from_vtt(content)
    segments: list[dict[str, Any]] = []
    lines = content.splitlines()
    i = 0
    seg_id = 0

    while i < len(lines):
        line = lines[i].strip()
        cue_match = _VTT_CUE_RE.match(line)
        if cue_match:
            start = _parse_timestamp(cue_match.group(1))
            end = _parse_timestamp(cue_match.group(2))
            i += 1
            text_lines: list[str] = []
            while i < len(lines) and lines[i].strip():
                text_lines.append(lines[i].strip())
                i += 1
            raw_text = " ".join(text_lines)
            text = _clean_text(raw_text)
            if text:
                segments.append(
                    {
                        "id": seg_id,
                        "start": round(start, 2),
                        "end": round(end, 2),
                        "text": text,
                    }
                )
                seg_id += 1
        else:
            i += 1

    return segments, language


def _parse_srt(content: str) -> list[dict[str, Any]]:
    """Parse de arquivo SRT. Retorna lista de segmentos."""
    segments: list[dict[str, Any]] = []
    blocks = re.split(r"\n\s*\n", content.strip())
    seg_id = 0

    for block in blocks:
        block_lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
        if not block_lines:
            continue

        # Pula linha de índice numérico
        start_idx = 0
        if block_lines[0].isdigit():
            start_idx = 1

        if start_idx >= len(block_lines):
            continue

        cue_match = _SRT_CUE_RE.match(block_lines[start_idx])
        if not cue_match:
            continue

        start = _parse_timestamp(cue_match.group(1))
        end = _parse_timestamp(cue_match.group(2))
        text_lines = block_lines[start_idx + 1 :]
        text = _clean_text(" ".join(text_lines))

        if text:
            segments.append(
                {
                    "id": seg_id,
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "text": text,
                }
            )
            seg_id += 1

    return segments


def _merge_overlapping_segments(
    segments: list[dict[str, Any]],
    *,
    overlap_threshold_seconds: float = 0.5,
) -> list[dict[str, Any]]:
    """
    Mescla segmentos sobrepostos ou muito próximos que o YouTube gera no VTT automático.
    Isso evita duplicação de texto entre segmentos consecutivos.
    """
    if not segments:
        return []

    merged: list[dict[str, Any]] = [dict(segments[0])]
    for seg in segments[1:]:
        prev = merged[-1]
        overlap = prev["end"] - seg["start"]
        # Segmentos com grande sobreposição ou texto idêntico são descartados
        if overlap > overlap_threshold_seconds or prev["text"] == seg["text"]:
            # Estende o end do segmento anterior se necessário
            if seg["end"] > prev["end"]:
                prev["end"] = round(seg["end"], 2)
            continue
        merged.append(dict(seg))

    # Renumera os IDs
    for idx, seg in enumerate(merged):
        seg["id"] = idx

    return merged


def parse_subtitle_to_transcript(subtitle_path: str, job_id: int) -> str:
    """
    Converte um arquivo de legenda VTT ou SRT para o formato JSON de transcrição do projeto
    e o salva em `transcripts/job_{job_id}.json`.

    Retorna o path do arquivo JSON gerado.
    """
    path = Path(subtitle_path)
    if not path.exists():
        raise FileNotFoundError(f"Arquivo de legenda não encontrado: {path}")

    content = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()

    if suffix == ".vtt":
        segments, language = _parse_vtt(content)
        segments = _merge_overlapping_segments(segments)
    elif suffix == ".srt":
        segments = _parse_srt(content)
        language = None
    else:
        # Tenta VTT por default (yt-dlp costuma baixar .vtt)
        logger.warning("Extensão desconhecida '%s', tentando parse VTT.", suffix)
        segments, language = _parse_vtt(content)
        segments = _merge_overlapping_segments(segments)

    if not segments:
        raise ValueError(f"Nenhum segmento encontrado na legenda: {path}")

    full_text = " ".join(seg["text"] for seg in segments if seg.get("text"))

    transcript_data: dict[str, Any] = {
        "job_id": job_id,
        "source": "youtube_subtitle",
        "subtitle_path": str(path),
        "language": language,
        "text": full_text.strip(),
        "segments_count": len(segments),
        "segments": segments,
    }

    output_path = get_storage().path_for(
        normalize_storage_key("transcripts", f"job_{job_id}.json")
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(transcript_data, f, ensure_ascii=False, indent=2)

    get_storage().sync_path(output_path)
    logger.info(
        "Legenda convertida: job_id=%s segments=%s language=%s output=%s",
        job_id,
        len(segments),
        language,
        output_path,
    )
    return str(output_path)
