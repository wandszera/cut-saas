import importlib.util
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.db.database import SessionLocal
from app.models.job import Job
from app.models.job_step import JobStep
from app.services.pipeline import ACTIVE_PIPELINE_JOB_STATUSES
from app.services.storage import get_storage
from app.services.transcription import _resolve_transcription_provider
from app.utils.runtime_env import detect_node


def _status(ok: bool, label_ok: str = "ok", label_fail: str = "erro") -> str:
    return label_ok if ok else label_fail


def _detect_ffmpeg() -> dict[str, Any]:
    resolved = shutil.which("ffmpeg")
    return {
        "name": "FFmpeg",
        "ok": bool(resolved),
        "status": _status(bool(resolved)),
        "detail": resolved or "ffmpeg nao encontrado no PATH",
    }


def _detect_ffprobe() -> dict[str, Any]:
    resolved = shutil.which("ffprobe")
    return {
        "name": "FFprobe",
        "ok": bool(resolved),
        "status": _status(bool(resolved)),
        "detail": resolved or "ffprobe nao encontrado no PATH",
    }


def _detect_whisper() -> dict[str, Any]:
    available = importlib.util.find_spec("whisper") is not None
    configured_provider = settings.transcription_provider
    resolved_provider = _resolve_transcription_provider()
    faster_available = importlib.util.find_spec("faster_whisper") is not None
    python_compatible_with_faster = sys.version_info < (3, 13)
    detail_parts = [
        f"provider={configured_provider}",
        f"resolved_provider={resolved_provider}",
        f"model={settings.whisper_model}",
        f"python={sys.version_info.major}.{sys.version_info.minor}",
    ]
    if configured_provider in {"auto", "faster_whisper"} and not python_compatible_with_faster:
        detail_parts.append("python_incompativel_para_faster_whisper")
    if configured_provider in {"auto", "faster_whisper"} and not faster_available:
        detail_parts.append("faster_whisper indisponivel")
    return {
        "name": "Transcricao",
        "ok": available,
        "status": _status(available),
        "detail": " | ".join(detail_parts) if available else "biblioteca whisper nao instalada",
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
    detail = info.get("version") if ok else (info.get("resolved_path") or info.get("error") or "indisponivel")
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
        "detail": f"provider={provider or 'nao definido'} | model={settings.llm_model}",
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


def _storage_readiness_item() -> dict[str, Any]:
    storage = get_storage()
    if settings.storage_backend == "local":
        base_dir = Path(settings.base_data_dir)
        ok = base_dir.exists() and base_dir.is_dir()
        return _build_readiness_item(
            name="Storage access",
            ok=ok,
            detail=f"backend=local | base_dir={base_dir.resolve()}",
            status_fail="erro",
        )

    bucket = settings.storage_bucket or "nao configurado"
    return _build_readiness_item(
        name="Storage access",
        ok=bool(bucket and bucket != "nao configurado"),
        detail=f"backend={settings.storage_backend} | bucket={bucket}",
        status_fail="erro",
    )


def _heartbeat_age_seconds(raw_value: str | None) -> float | None:
    if not raw_value:
        return None
    try:
        heartbeat_dt = datetime.fromisoformat(str(raw_value))
    except ValueError:
        return None
    if heartbeat_dt.tzinfo is None:
        heartbeat_dt = heartbeat_dt.replace(tzinfo=UTC)
    return (datetime.now(UTC) - heartbeat_dt.astimezone(UTC)).total_seconds()


def _worker_backlog_readiness_item() -> dict[str, Any]:
    try:
        with SessionLocal() as db:
            pending_count = db.query(Job).filter(Job.status == "pending").count()
            active_count = db.query(Job).filter(Job.status.in_(ACTIVE_PIPELINE_JOB_STATUSES)).count()
            running_steps = db.query(JobStep).filter(JobStep.status == "running").all()
    except SQLAlchemyError as exc:
        return _build_readiness_item(
            name="Worker backlog",
            ok=False,
            detail=f"falha ao inspecionar fila: {exc}",
            status_fail="erro",
        )

    stale_steps = 0
    for step in running_steps:
        details = step.details or ""
        heartbeat_at = None
        if '"heartbeat_at"' in details:
            try:
                import json
                payload = json.loads(details)
                if isinstance(payload, dict):
                    heartbeat_at = payload.get("heartbeat_at")
            except Exception:
                heartbeat_at = None
        age = _heartbeat_age_seconds(heartbeat_at)
        if age is not None and age >= 900:
            stale_steps += 1

    ok = stale_steps == 0
    detail = (
        f"pending_jobs={pending_count} | active_jobs={active_count} | "
        f"running_steps={len(running_steps)} | stale_running_steps={stale_steps}"
    )
    return _build_readiness_item(
        name="Worker backlog",
        ok=ok,
        detail=detail,
        status_fail="erro",
    )


def _build_readiness_item(*, name: str, ok: bool, detail: str, status_ok: str = "ok", status_fail: str = "pendente") -> dict[str, Any]:
    return {
        "name": name,
        "ok": ok,
        "status": _status(ok, status_ok, status_fail),
        "detail": detail,
    }


def _transcription_runtime_compatibility_item() -> dict[str, Any]:
    configured_provider = settings.transcription_provider
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    faster_available = importlib.util.find_spec("faster_whisper") is not None
    python_compatible_with_faster = sys.version_info < (3, 13)

    if configured_provider == "openai_whisper":
        return _build_readiness_item(
            name="Transcription runtime",
            ok=True,
            detail=f"provider=openai_whisper | python={python_version}",
        )

    if python_compatible_with_faster and faster_available:
        return _build_readiness_item(
            name="Transcription runtime",
            ok=True,
            detail=f"provider={configured_provider} | python={python_version} | faster_whisper disponivel",
        )

    detail = f"provider={configured_provider} | python={python_version}"
    if not python_compatible_with_faster:
        detail += " | requer Python 3.11 ou 3.12 para faster_whisper"
    if not faster_available:
        detail += " | faster_whisper nao instalado"
    return _build_readiness_item(
        name="Transcription runtime",
        ok=False,
        detail=detail,
    )


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
        _transcription_runtime_compatibility_item(),
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


def build_runtime_readiness() -> dict[str, Any]:
    checks = [
        _detect_database(),
        _detect_node(),
        _detect_ffmpeg(),
        _detect_ffprobe(),
        _detect_whisper(),
        _storage_readiness_item(),
        _worker_backlog_readiness_item(),
        _build_readiness_item(
            name="Worker queue mode",
            ok=settings.pipeline_queue_backend in {"local", "worker"},
            detail=f"PIPELINE_QUEUE_BACKEND={settings.pipeline_queue_backend}",
            status_fail="erro",
        ),
    ]
    return {
        "ready": all(item["ok"] for item in checks),
        "checks_ok": sum(1 for item in checks if item["ok"]),
        "checks_total": len(checks),
        "checks": checks,
    }


def build_system_diagnostics() -> dict[str, Any]:
    checks = [
        _detect_database(),
        _detect_node(),
        _detect_ffmpeg(),
        _detect_ffprobe(),
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
            "transcription_provider": settings.transcription_provider,
            "whisper_model": settings.whisper_model,
            "whisper_precision": settings.whisper_precision,
            "llm_provider": settings.llm_provider,
            "llm_model": settings.llm_model,
            "llm_rerank_enabled": settings.llm_rerank_enabled,
            "node_bin": settings.node_bin,
            "ollama_base_url": settings.ollama_base_url,
            "openai_base_url": settings.openai_base_url,
        },
        "deployment_readiness": _detect_deployment_readiness(),
        "runtime_readiness": build_runtime_readiness(),
    }
