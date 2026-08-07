"""Task 5: the WebSocket handshake is limited before it is accepted.

The module-level ``ws_upgrade_limiter`` is disabled here, because conftest.py
sets RATE_LIMIT_ENABLED=false for the whole suite. Driving it would therefore
prove nothing at all -- which is exactly the blind spot that let the key
function bug in tests/test_ratelimit_key.py reach production. So enforcement
is tested against locally constructed limiters with in-memory storage, and the
shared instance is asserted only on its configuration.
"""

import time
from typing import Any

from starlette.websockets import WebSocket

from app.core.ratelimit import (
    ADMIN_LIMIT,
    WS_UPGRADE_LIMIT,
    WebSocketUpgradeLimiter,
    websocket_key,
    ws_upgrade_limiter,
)

PEER = "198.51.100.7"
REAL_CLIENT = "203.0.113.9"
OTHER_CLIENT = "203.0.113.10"


async def _receive() -> dict[str, Any]:
    return {"type": "websocket.connect"}


async def _send(message: dict[str, Any]) -> None:
    return None


def _websocket(forwarded_for: str | None = None) -> WebSocket:
    """A handshake scope, built the way tests/test_ratelimit_key.py builds one."""
    headers = []
    if forwarded_for is not None:
        headers.append((b"x-forwarded-for", forwarded_for.encode()))
    scope = {
        "type": "websocket",
        "path": "/ws/events",
        "raw_path": b"/ws/events",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": (PEER, 51234),
        "server": ("testserver", 80),
    }
    return WebSocket(scope, receive=_receive, send=_send)


def _limiter(limit: str) -> WebSocketUpgradeLimiter:
    return WebSocketUpgradeLimiter(limit, storage_uri="memory://", enabled=True)


class TestHandshakeKey:
    """A handshake must resolve to the same bucket an HTTP request would."""

    def test_falls_back_to_the_socket_peer_without_a_header(self) -> None:
        assert websocket_key(_websocket()) == PEER

    def test_uses_the_entry_written_by_our_own_proxy(self) -> None:
        assert websocket_key(_websocket(f"1.2.3.4, {REAL_CLIENT}")) == REAL_CLIENT

    def test_a_forged_prefix_cannot_change_the_bucket(self) -> None:
        """Otherwise the limit is bypassed by varying one header per attempt."""
        first = websocket_key(_websocket(f"10.0.0.1, {REAL_CLIENT}"))
        second = websocket_key(_websocket(f"172.16.0.1, 8.8.8.8, {REAL_CLIENT}"))
        assert first == second == REAL_CLIENT


class TestEnforcement:
    def test_upgrades_are_allowed_up_to_the_limit(self) -> None:
        limiter = _limiter("5/minute")
        assert all(limiter.allow(REAL_CLIENT) for _ in range(5))

    def test_the_limit_is_enforced(self) -> None:
        limiter = _limiter("5/minute")
        for _ in range(5):
            limiter.allow(REAL_CLIENT)
        assert limiter.allow(REAL_CLIENT) is False

    def test_one_client_cannot_exhaust_another_clients_allowance(self) -> None:
        """Reconnects must survive somebody else being throttled."""
        limiter = _limiter("2/minute")
        limiter.allow(REAL_CLIENT)
        limiter.allow(REAL_CLIENT)
        assert limiter.allow(REAL_CLIENT) is False
        assert limiter.allow(OTHER_CLIENT) is True

    def test_the_window_resets(self) -> None:
        """A throttled operator gets back in without restarting anything.

        Sleeps rather than mocking the clock: the window boundary is computed
        inside the limits library, so a fake clock here would test nothing but
        the fake.
        """
        limiter = _limiter("2/second")
        limiter.allow(REAL_CLIENT)
        limiter.allow(REAL_CLIENT)
        assert limiter.allow(REAL_CLIENT) is False
        time.sleep(1.1)
        assert limiter.allow(REAL_CLIENT) is True

    def test_reset_clears_a_window(self) -> None:
        limiter = _limiter("1/minute")
        assert limiter.allow(REAL_CLIENT) is True
        assert limiter.allow(REAL_CLIENT) is False
        limiter.reset(REAL_CLIENT)
        assert limiter.allow(REAL_CLIENT) is True


class TestSharedInstance:
    def test_the_handshake_borrows_the_admin_convention(self) -> None:
        """Not a new number: the same allowance the admin API already uses."""
        assert WS_UPGRADE_LIMIT == ADMIN_LIMIT

    def test_a_disabled_limiter_allows_everything(self) -> None:
        """How the shared instance behaves in this suite, and in local dev.

        Also the property that keeps the route's behaviour unchanged for every
        other test in the suite: with RATE_LIMIT_ENABLED=false the check in
        dashboard_events is a straight pass-through.
        """
        disabled = WebSocketUpgradeLimiter(
            "1/minute", storage_uri="memory://", enabled=False
        )
        assert all(disabled.allow(REAL_CLIENT) for _ in range(10))

    def test_the_shared_limiter_is_disabled_under_pytest(self) -> None:
        assert ws_upgrade_limiter.enabled is False
        assert ws_upgrade_limiter.limit == ADMIN_LIMIT
