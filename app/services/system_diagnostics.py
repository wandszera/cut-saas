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


def _build_readiness_item(*, name: str, ok: bool, detail: str, status_ok: str = "ok", status_fail: str = "pendente") -> dict[str, Any]:
    return {
        "name": name,
        "ok": ok,
        "status": _status(ok, status_ok, status_fail),
        "detail": detail,
    }


def _detect_deployment_readiness() -> dict[str, Any]:
    checks = [
        _build_readiness_item(
            name="Environment",
            ok=settings.environment in {"staging", "production"},
            detail=f"ENVIRONMENT={settings.environment}",
        ),
        _build_readiness_item(
            name="Database",
            ok=settings.is_deployed_environment and settings.database_url_for_engine.startswith("postgresql+psycopg://"),
            detail=settings.database_url_for_engine,
        ),
        _build_readiness_item(
            name="Alembic",
            ok=Path("alembic.ini").exists() and Path("alembic/env.py").exists(),
            detail="alembic.ini e alembic/env.py precisam existir para upgrade head",
        ),
        _build_readiness_item(
            name="Secret key",
            ok=settings.secret_key != "dev-secret-change-me" and len(settings.secret_key) >= 32,
            detail="SECRET_KEY deve ser unico e ter pelo menos 32 caracteres",
        ),
        _build_readiness_item(
            name="Session cookie",
            ok=settings.environment != "production" or settings.session_cookie_secure,
            detail=f"SESSION_COOKIE_SECURE={settings.session_cookie_secure}",
        ),
        _build_readiness_item(
            name="Queue backend",
            ok=settings.pipeline_queue_backend == "worker",
            detail=f"PIPELINE_QUEUE_BACKEND={settings.pipeline_queue_backend}",
        ),
        _build_readiness_item(
            name="Storage backend",
            ok=settings.storage_backend != "local",
            detail=f"STORAGE_BACKEND={settings.storage_backend}",
        ),
        _build_readiness_item(
            name="Billing provider",
            ok=settings.billing_provider != "mock",
            detail=f"BILLING_PROVIDER={settings.billing_provider}",
        ),
    ]

    ready = all(item["ok"] for item in checks)
    return {
        "target_environment": settings.environment,
        "ready": ready,
        "checks_ok": sum(1 for item in checks if item["ok"]),
        "checks_total": len(checks),
        "checks": checks,
        "next_steps": [
            "Rodar alembic upgrade head antes de subir a API em staging.",
            "Iniciar a API e o worker em processos separados.",
            "Validar login, criacao de job, processamento e download assinado com dados reais.",
        ],
    }


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
            "environment": settings.environment,
            "database_url": settings.database_url,
            "base_data_dir": settings.base_data_dir,
            "storage_backend": settings.storage_backend,
            "billing_provider": settings.billing_provider,
            "pipeline_queue_backend": settings.pipeline_queue_backend,
            "whisper_model": settings.whisper_model,
            "llm_provider": settings.llm_provider,
            "llm_model": settings.llm_model,
            "llm_rerank_enabled": settings.llm_rerank_enabled,
            "node_bin": settings.node_bin,
            "ollama_base_url": settings.ollama_base_url,
            "openai_base_url": settings.openai_base_url,
        },
        "deployment_readiness": _detect_deployment_readiness(),
    }
