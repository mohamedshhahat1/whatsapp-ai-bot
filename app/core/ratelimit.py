"""Redis-backed rate limiting shared across all app replicas (slowapi).

When rate limiting is disabled (tests / local development), an in-memory
storage is used and slowapi skips all checks entirely.

Scope note: this module limits HTTP *endpoints* and the WebSocket handshake.
It is not, and cannot be, per-customer fairness -- see ``app/core/quota.py``
for that. The two are complementary and neither replaces the other.

A note on the parameter name
----------------------------
Both key functions below MUST name their parameter ``request``, even where the
value is unused. slowapi chooses how to call them by inspecting the signature:

    if "request" in signature(lim.key_func).parameters.keys():
        limit_key = lim.key_func(request)
    else:
        limit_key = lim.key_func()

So a parameter renamed to ``_request`` to signal "unused" makes slowapi call it
with no arguments, which raises TypeError inside the limiter and returns 500
for every request to the decorated route. Nothing in the type system or the
linter can see that; the name is the interface. See
tests/test_ratelimit_key.py.

``websocket_key`` is exempt from that rule because slowapi never calls it --
the handshake is checked by hand, for the reason given on
``WebSocketUpgradeLimiter``.
"""

from limits import parse
from limits.storage import storage_from_string
from limits.strategies import FixedWindowRateLimiter
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.websockets import WebSocket

from app.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

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

# Namespace for handshake counters, so they cannot collide with the per-route
# buckets slowapi keeps in the same storage.
WS_UPGRADE_BUCKET = "ws-upgrade"


def _key_from(headers: Headers, peer: str) -> str:
    """Derive the rate-limit key from a request's or handshake's origin.

    Shared by :func:`client_key` and :func:`websocket_key` so that both
    protocols resolve a client identically. Duplicating it would mean the
    X-Forwarded-For hardening below could be fixed in one place and quietly
    left broken in the other.
    """
    hops = _settings.trusted_proxy_hops
    if hops > 0:
        forwarded = headers.get("X-Forwarded-For", "")
        chain = [part.strip() for part in forwarded.split(",") if part.strip()]
        if len(chain) >= hops:
            return chain[-hops]
    return peer


def client_key(request: Request) -> str:
    """Rate-limit key: the real client IP.

    ``X-Forwarded-For`` is client-controlled. Our nginx appends the peer
    address to whatever the client sent, so the left-most entry is forgeable
    and taking it hands every request a brand new bucket -- the rate limit
    stops existing. Only the last ``TRUSTED_PROXY_HOPS`` entries were written
    by infrastructure we control, so the real client is that many positions
    from the right.
    """
    return _key_from(request.headers, get_remote_address(request))


def websocket_key(websocket: WebSocket) -> str:
    """The same key, derived from a WebSocket handshake.

    A handshake carries the same headers and the same peer address as an HTTP
    request -- it *is* an HTTP request until the upgrade completes -- but it
    is not a Starlette ``Request``, and one cannot be constructed from a scope
    whose type is "websocket". Hence a sibling rather than a cast.
    """
    peer = websocket.client.host if websocket.client else "127.0.0.1"
    return _key_from(websocket.headers, peer)


def webhook_key(request: Request) -> str:
    """Constant key for Meta's webhook.

    Deliberately ignores the request -- but the parameter must still be called
    ``request``, because slowapi reads the name to decide whether to pass one.
    See the module docstring; renaming it to ``_request`` breaks every webhook
    delivery with a 500.

    The limit this produces is a crude ceiling on total inbound volume --
    protection against a broken sender or a flood of unsigned junk -- and
    nothing to do with fairness between customers. It is set high enough (see
    ``rate_limit_webhook``) that normal business traffic can never reach it,
    because reaching it drops real customers' messages.

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

# The handshake is an admin action, so it borrows the admin allowance rather
# than inventing a number. A dashboard holds one socket open and reconnects
# only when it drops, so even a deploy that restarts every replica and sends
# every browser into backoff stays well inside this.
WS_UPGRADE_LIMIT = ADMIN_LIMIT


class WebSocketUpgradeLimiter:
    """Fixed-window limit on WebSocket handshakes.

    Separate from the slowapi ``limiter`` above because slowapi's decorator
    takes a ``Request`` and a WebSocket route never has one. The strategy,
    the storage and the enabled/disabled conditions are deliberately the same,
    so the two behave identically and are configured by the same settings.

    Storage is injected rather than read from settings so that the behaviour
    can be tested at all: conftest.py sets RATE_LIMIT_ENABLED=false, so a test
    driving the module-level instance would exercise nothing but the early
    return. tests/test_ratelimit_key.py records what that blind spot cost the
    last time it was left uncovered.
    """

    def __init__(self, limit: str, *, storage_uri: str, enabled: bool) -> None:
        self.enabled = enabled
        self.limit = limit
        self._item = parse(limit)
        self._limiter = FixedWindowRateLimiter(storage_from_string(storage_uri))

    def allow(self, key: str) -> bool:
        """Consume one handshake from ``key``'s allowance.

        Fails open. If the storage is unreachable the dashboard should still
        connect: the alternative is that a Redis outage locks every operator
        out of the tool they would use to notice it. The events stream already
        degrades to polling under exactly this condition.
        """
        if not self.enabled:
            return True
        try:
            return self._limiter.hit(self._item, WS_UPGRADE_BUCKET, key)
        except Exception:
            logger.warning("ws_upgrade_limit_unavailable", exc_info=True)
            return True

    def reset(self, key: str) -> None:
        """Drop ``key``'s current window. Used by tests."""
        self._limiter.clear(self._item, WS_UPGRADE_BUCKET, key)


ws_upgrade_limiter = WebSocketUpgradeLimiter(
    WS_UPGRADE_LIMIT,
    storage_uri=_settings.redis_url if _settings.rate_limit_enabled else "memory://",
    enabled=_settings.rate_limit_enabled,
)
