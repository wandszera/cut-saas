"""Inicialização centralizada do Sentry para API e Worker."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def init_sentry(*, integrations: list | None = None) -> bool:
    """Inicializa o Sentry SDK se SENTRY_DSN estiver configurado.

    Retorna True se o Sentry foi inicializado, False caso contrário.
    Seguro para ambientes sem DSN — simplesmente não faz nada.
    """
    from app.core.config import settings

    if not settings.sentry_dsn:
        return False

    try:
        import sentry_sdk

        default_integrations: list = integrations or []

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.environment,
            release=None,  # defina via SENTRY_RELEASE env var se quiser pin de versão
            traces_sample_rate=settings.sentry_traces_sample_rate,
            profiles_sample_rate=settings.sentry_profiles_sample_rate,
            integrations=default_integrations,
            # Evita vazar dados sensíveis nas breadcrumbs
            send_default_pii=False,
            # Ignora 404s e 401s — não são bugs, são uso normal
            before_send=_before_send,
        )

        logger.info(
            "Sentry inicializado | env=%s traces=%.2f profiles=%.2f",
            settings.environment,
            settings.sentry_traces_sample_rate,
            settings.sentry_profiles_sample_rate,
        )
        return True

    except ImportError:
        logger.warning("sentry-sdk não está instalado — monitoramento desativado.")
        return False
    except Exception as exc:  # pragma: no cover
        logger.warning("Falha ao inicializar Sentry: %s", exc)
        return False


def _before_send(event: dict, hint: dict) -> dict | None:
    """Filtra eventos antes de enviá-los ao Sentry.

    - Descarta erros HTTP 404/401/403 que são comportamento esperado.
    - Descarta CancelledError (desconexão do cliente) que geram ruído.
    """
    exc_info = hint.get("exc_info")
    if exc_info is not None:
        exc_type, exc_value, _ = exc_info

        # Descartar HTTPException com status 401, 403, 404
        if exc_type is not None and exc_type.__name__ == "HTTPException":
            status_code = getattr(exc_value, "status_code", None)
            if status_code in {401, 403, 404}:
                return None

        # Descartar CancelledError (cliente desconectou durante a resposta)
        if exc_type is not None and exc_type.__name__ in {
            "CancelledError",
            "asyncio.CancelledError",
        }:
            return None

    return event


def capture_exception(exc: BaseException) -> None:
    """Envia uma exceção manualmente ao Sentry (se inicializado)."""
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
    except ImportError:
        pass


def capture_message(message: str, level: str = "info") -> None:
    """Envia uma mensagem manual ao Sentry (se inicializado)."""
    try:
        import sentry_sdk

        sentry_sdk.capture_message(message, level=level)
    except ImportError:
        pass
