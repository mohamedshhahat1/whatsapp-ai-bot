"""Redis-backed rate limiting shared across all app replicas (slowapi).

When rate limiting is disabled (tests / local development), an in-memory
storage is used and slowapi skips all checks entirely.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.config import get_settings

_settings = get_settings()


def client_key(request: Request) -> str:
    """Rate-limit key: the real client IP, honoring the reverse proxy header."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(
    key_func=client_key,
    storage_uri=_settings.redis_url if _settings.rate_limit_enabled else "memory://",
    enabled=_settings.rate_limit_enabled,
    strategy="fixed-window",
)

WEBHOOK_LIMIT = _settings.rate_limit_webhook
ADMIN_LIMIT = _settings.rate_limit_admin
