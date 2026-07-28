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
    """Rate-limit key: the real client IP.

    ``X-Forwarded-For`` is client-controlled. Our nginx appends the peer
    address to whatever the client sent, so the left-most entry is forgeable
    and taking it hands every request a brand new bucket -- the rate limit
    stops existing. Only the last ``TRUSTED_PROXY_HOPS`` entries were written
    by infrastructure we control, so the real client is that many positions
    from the right.
    """
    hops = _settings.trusted_proxy_hops
    if hops > 0:
        forwarded = request.headers.get("X-Forwarded-For", "")
        chain = [part.strip() for part in forwarded.split(",") if part.strip()]
        if len(chain) >= hops:
            return chain[-hops]
    return get_remote_address(request)


limiter = Limiter(
    key_func=client_key,
    storage_uri=_settings.redis_url if _settings.rate_limit_enabled else "memory://",
    enabled=_settings.rate_limit_enabled,
    strategy="fixed-window",
)

WEBHOOK_LIMIT = _settings.rate_limit_webhook
ADMIN_LIMIT = _settings.rate_limit_admin
