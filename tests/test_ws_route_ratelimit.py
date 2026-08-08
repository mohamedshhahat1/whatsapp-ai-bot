"""Route-level proof that /ws/events refuses an over-limit upgrade.

tests/test_ws_ratelimit.py covers WebSocketUpgradeLimiter and websocket_key()
as units: it builds a limiter, calls allow(), and checks the answer. None of
that touches the route, so it could not say whether dashboard_events consults
the limiter at all, whether it uses the key it computed, or whether a refusal
reaches the client as an HTTP 429 instead of an accepted socket closed a
moment later.

Everything here goes through the real ASGI app. The distinction these tests
lean on is that a refused upgrade raises WebSocketDenialResponse -- the
handshake never completed -- while an accepted-then-rejected one raises
WebSocketDisconnect with a close code. Failed authentication produces the
second, so a test that only ever saw a disconnect would prove nothing about
rate limiting.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.core.ratelimit import WebSocketUpgradeLimiter, websocket_key
from app.routers import events

# TestClient presents itself as this host, so it is also the bucket the route
# should be filling.
TESTCLIENT_HOST = "testclient"


def _limiter(limit: str) -> WebSocketUpgradeLimiter:
    """A real limiter, in memory, switched on.

    RATE_LIMIT_ENABLED is false under pytest, so the module-level limiter
    always allows. These tests install their own rather than turning rate
    limiting on for the whole suite.
    """
    return WebSocketUpgradeLimiter(limit, storage_uri="memory://", enabled=True)


def _attempt_upgrade(client: TestClient) -> int:
    """Complete one upgrade attempt and return the close code.

    Getting a close code at all means the handshake succeeded and the route
    called accept(); the deliberately wrong key is then refused by
    _authenticate. An upgrade the limiter refused never gets this far.
    """
    with pytest.raises(WebSocketDisconnect) as disconnected:
        with client.websocket_connect("/ws/events") as websocket:
            websocket.send_json({"api_key": "not-the-admin-key"})
            websocket.receive_text()
    return disconnected.value.code


def _raw_websocket(
    sent: list[dict[str, Any]],
    *,
    peer: tuple[str, int] = ("198.51.100.7", 51234),
    extensions: dict[str, Any] | None = None,
) -> WebSocket:
    """A WebSocket with no ASGI server behind it.

    Used to drive _refuse_upgrade directly. Whether the denial-response
    extension is advertised decides which of its two branches runs, which is
    the whole point of the two tests that use it.
    """

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope: dict[str, Any] = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "path": "/ws/events",
        "raw_path": b"/ws/events",
        "query_string": b"",
        "root_path": "",
        "scheme": "ws",
        "headers": [],
        "client": peer,
        "server": ("testserver", 80),
    }
    if extensions is not None:
        scope["extensions"] = extensions
    return WebSocket(scope, receive=receive, send=send)


def test_an_allowed_upgrade_reaches_authentication(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control case, without which a 429 test proves nothing.

    If the route refused every upgrade, or the app rejected the handshake for
    some unrelated reason, the over-limit test below would still see a denial
    response and still pass.
    """
    monkeypatch.setattr(events, "ws_upgrade_limiter", _limiter("5/minute"))

    assert _attempt_upgrade(client) == events.POLICY_VIOLATION


def test_an_over_limit_upgrade_gets_429(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two through, the third refused at the handshake.

    WebSocketDenialResponse rather than WebSocketDisconnect is the proof that
    accept() was never called: the client received an HTTP response instead
    of a completed upgrade.
    """
    monkeypatch.setattr(events, "ws_upgrade_limiter", _limiter("2/minute"))

    assert _attempt_upgrade(client) == events.POLICY_VIOLATION
    assert _attempt_upgrade(client) == events.POLICY_VIOLATION

    with pytest.raises(WebSocketDenialResponse) as refused:
        with client.websocket_connect("/ws/events"):
            pass

    assert refused.value.status_code == 429
    assert refused.value.text == "Too Many Requests"


def test_the_route_buckets_by_the_websocket_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves the route uses websocket_key(), not some other bucket.

    The limiter is exhausted, then cleared for exactly the key
    websocket_key() derives from a TestClient handshake. Had the route keyed
    its bucket on anything else, clearing that one key would not let the next
    upgrade through.
    """
    limiter = _limiter("1/minute")
    monkeypatch.setattr(events, "ws_upgrade_limiter", limiter)

    assert _attempt_upgrade(client) == events.POLICY_VIOLATION

    with pytest.raises(WebSocketDenialResponse):
        with client.websocket_connect("/ws/events"):
            pass

    limiter.reset(TESTCLIENT_HOST)

    assert _attempt_upgrade(client) == events.POLICY_VIOLATION


def test_websocket_key_names_the_testclient_bucket() -> None:
    """Ties the constant above to the production key function."""
    websocket = _raw_websocket([], peer=(TESTCLIENT_HOST, 50000))

    assert websocket_key(websocket) == TESTCLIENT_HOST


async def test_a_refusal_sends_a_429_denial_response() -> None:
    """The refusal branch itself, which no test had ever executed."""
    sent: list[dict[str, Any]] = []
    websocket = _raw_websocket(sent, extensions={"websocket.http.response": {}})

    await events._refuse_upgrade(websocket)

    starts = [m for m in sent if m["type"] == "websocket.http.response.start"]
    bodies = [m for m in sent if m["type"] == "websocket.http.response.body"]

    assert [m["status"] for m in starts] == [429]
    assert b"".join(m.get("body", b"") for m in bodies) == b"Too Many Requests"
    assert not [m for m in sent if m["type"] == "websocket.accept"]


async def test_a_refusal_falls_back_to_close_1013() -> None:
    """No denial-response extension, so the refusal has to close instead.

    A server without the extension makes send_denial_response() raise
    RuntimeError. Without the fallback the refusal would surface as a 500 and
    the limiter would look broken.
    """
    sent: list[dict[str, Any]] = []
    websocket = _raw_websocket(sent)

    await events._refuse_upgrade(websocket)

    assert [m["type"] for m in sent] == ["websocket.close"]
    assert sent[0]["code"] == events.TRY_AGAIN_LATER
