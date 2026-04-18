import importlib.util
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.db.database import SessionLocal
from app.utils.runtime_env import detect_node


def _status(ok: bool, label_ok: str = "ok", label_fail: str = "erro") -> str:
    return label_ok if ok else label_fail


def _detect_ffmpeg() -> dict[str, Any]:
    resolved = shutil.which("ffmpeg")
    return {
        "name": "FFmpeg",
        "ok": bool(resolved),
        "status": _status(bool(resolved)),
        "detail": resolved or "ffmpeg não encontrado no PATH",
    }


def _detect_whisper() -> dict[str, Any]:
    available = importlib.util.find_spec("whisper") is not None
    return {
        "name": "Whisper",
        "ok": available,
        "status": _status(available),
        "detail": f"model={settings.whisper_model}" if available else "biblioteca whisper não instalada",
    }


def _detect_database() -> dict[str, Any]:
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        return {
            "name": "Banco",
            "ok": True,
            "status": "ok",
            "detail": settings.database_url,
        }
    except SQLAlchemyError as exc:
        return {
            "name": "Banco",
            "ok": False,
            "status": "erro",
            "detail": str(exc),
        }


def _detect_node() -> dict[str, Any]:
    info = detect_node()
    ok = bool(info.get("available"))
    detail = info.get("version") if ok else (info.get("resolved_path") or info.get("error") or "indisponível")
    return {
      "name": "Node.js",
      "ok": ok,
      "status": _status(ok),
      "detail": detail,
    }


def _detect_llm() -> dict[str, Any]:
    provider = (settings.llm_provider or "").strip().lower()
    if provider == "openai":
        ok = bool(settings.openai_api_key)
        detail = f"provider=openai | model={settings.llm_model}"
        if not ok:
            detail += " | OPENAI_API_KEY ausente"
        return {
            "name": "LLM",
            "ok": ok,
            "status": _status(ok, "configurado", "incompleto"),
            "detail": detail,
        }

    ok = bool(provider)
    return {
        "name": "LLM",
        "ok": ok,
        "status": _status(ok, "configurado", "incompleto"),
        "detail": f"provider={provider or 'não definido'} | model={settings.llm_model}",
    }


def _detect_directories() -> list[dict[str, Any]]:
    base = Path(settings.base_data_dir)
    rows = []
    for folder in ["downloads", "transcripts", "clips", "temp", "exports", "uploads"]:
        path = base / folder
        rows.append(
            {
                "name": folder,
                "ok": path.exists(),
                "status": _status(path.exists()),
                "detail": str(path.resolve()),
            }
        )
    return rows


def build_system_diagnostics() -> dict[str, Any]:
    checks = [
        _detect_database(),
        _detect_node(),
        _detect_ffmpeg(),
        _detect_whisper(),
        _detect_llm(),
    ]
    directories = _detect_directories()
    all_ok = all(item["ok"] for item in checks) and all(item["ok"] for item in directories)
    return {
        "overall_ok": all_ok,
        "summary": {
            "checks_ok": sum(1 for item in checks if item["ok"]),
            "checks_total": len(checks),
            "directories_ok": sum(1 for item in directories if item["ok"]),
            "directories_total": len(directories),
            "base_data_dir": str(Path(settings.base_data_dir).resolve()),
        },
        "checks": checks,
        "directories": directories,
        "settings_snapshot": {
            "database_url": settings.database_url,
            "base_data_dir": settings.base_data_dir,
            "whisper_model": settings.whisper_model,
            "llm_provider": settings.llm_provider,
            "llm_model": settings.llm_model,
            "llm_rerank_enabled": settings.llm_rerank_enabled,
            "node_bin": settings.node_bin,
            "ollama_base_url": settings.ollama_base_url,
            "openai_base_url": settings.openai_base_url,
        },
    }
