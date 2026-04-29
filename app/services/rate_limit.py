import threading
import time
from collections import deque
from dataclasses import dataclass

from fastapi import HTTPException, Request


@dataclass(frozen=True)
class RateLimitRule:
    key_prefix: str
    limit: int
    window_seconds: int
    detail: str


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def check(self, key: str, *, limit: int, window_seconds: int) -> None:
        now = time.time()
        window_start = now - window_seconds
        with self._lock:
            bucket = self._events.setdefault(key, deque())
            while bucket and bucket[0] <= window_start:
                bucket.popleft()
            if len(bucket) >= limit:
                raise HTTPException(status_code=429, detail="Muitas tentativas. Tente novamente em instantes.")
            bucket.append(now)


rate_limiter = InMemoryRateLimiter()


AUTH_LOGIN_RULE = RateLimitRule(
    key_prefix="login",
    limit=5,
    window_seconds=300,
    detail="Muitas tentativas de login. Aguarde alguns minutos antes de tentar novamente.",
)
AUTH_REGISTER_RULE = RateLimitRule(
    key_prefix="register",
    limit=3,
    window_seconds=600,
    detail="Muitas tentativas de cadastro. Aguarde alguns minutos antes de tentar novamente.",
)
BILLING_MUTATION_RULE = RateLimitRule(
    key_prefix="billing-mutation",
    limit=10,
    window_seconds=300,
    detail="Muitas alteracoes de billing em pouco tempo. Tente novamente em instantes.",
)
BILLING_WEBHOOK_RULE = RateLimitRule(
    key_prefix="billing-webhook",
    limit=120,
    window_seconds=60,
    detail="Volume de webhooks acima do permitido no momento.",
)


def get_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip() or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def build_rate_limit_key(request: Request, rule: RateLimitRule, suffix: str = "") -> str:
    ip = get_client_ip(request)
    path = request.url.path
    normalized_suffix = suffix.strip() if suffix else ""
    return ":".join(part for part in (rule.key_prefix, ip, path, normalized_suffix) if part)


def enforce_rate_limit(request: Request, rule: RateLimitRule, *, suffix: str = "") -> None:
    key = build_rate_limit_key(request, rule, suffix=suffix)
    try:
        rate_limiter.check(key, limit=rule.limit, window_seconds=rule.window_seconds)
    except HTTPException as exc:
        raise HTTPException(status_code=429, detail=rule.detail) from exc
