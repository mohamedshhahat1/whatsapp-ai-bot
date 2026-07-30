"""Redis-backed rate limiting shared across all app replicas (slowapi).

When rate limiting is disabled (tests / local development), an in-memory
storage is used and slowapi skips all checks entirely.

Scope note: this module limits HTTP *endpoints*. It is not, and cannot be,
per-customer fairness -- see ``app/core/quota.py`` for that. The two are
complementary and neither replaces the other.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.config import get_settings

_settings = get_settings()

# One fixed bucket for every WhatsApp webhook delivery.
#
# The address is always Meta's, so keying by IP does not separate customers --
# it lumps the entire business into one or two buckets and then throttles them
# together. A single person holding down send would consume the allowance for
# everyone else, and the messages dropped would be the innocent ones.
#
# Naming the bucket makes that explicit rather than accidental, and removes the
# forgeable X-Forwarded-For lookup from the busiest path in the application.
META_WEBHOOK_BUCKET = "meta-webhook"


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


def webhook_key(_request: Request) -> str:
    """Constant key for Meta's webhook.

    Deliberately ignores the request. The limit this produces is a crude
    ceiling on total inbound volume -- protection against a broken sender or a
    flood of unsigned junk -- and nothing to do with fairness between
    customers. It is set high enough (see ``rate_limit_webhook``) that normal
    business traffic can never reach it, because reaching it drops real
    customers' messages.

    Per-customer limits are enforced in ``app/core/quota.py``, keyed by
    wa_id, which is only knowable after the payload has been parsed and its
    signature verified.
    """
    return META_WEBHOOK_BUCKET


limiter = Limiter(
    key_func=client_key,
    storage_uri=_settings.redis_url if _settings.rate_limit_enabled else "memory://",
    enabled=_settings.rate_limit_enabled,
    strategy="fixed-window",
)

WEBHOOK_LIMIT = _settings.rate_limit_webhook
ADMIN_LIMIT = _settings.rate_limit_admin
