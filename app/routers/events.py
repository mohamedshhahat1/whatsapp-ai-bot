"""WebSocket stream of dashboard events.

One connection per open dashboard. The endpoint subscribes to the Redis
channel that the worker and the admin API publish to, and forwards each event
to the browser.

Authentication needs a word of explanation. The browser WebSocket API cannot
set request headers, so ``X-API-Key`` is not available here. Passing the key as
a query parameter would write it into nginx access logs and browser history, so
the client sends it as the first frame instead and the server refuses to
subscribe until it matches.
"""

import asyncio
import contextlib
import hmac
import json
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.core.events import CHANNEL
from app.core.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(tags=["events"])

# Close codes. 1008 is "policy violation", which is what a failed handshake is.
POLICY_VIOLATION = 1008
INTERNAL_ERROR = 1011

# A client that connects and says nothing is either broken or probing.
AUTH_TIMEOUT_SECONDS = 5.0

# Idle WebSockets get closed by proxies. nginx is configured for a long read
# timeout, but a heartbeat also lets both sides notice a dead peer, and gives
# the browser something to distinguish "quiet" from "disconnected".
HEARTBEAT_SECONDS = 20.0

# How long to block waiting for a published event before checking whether a
# heartbeat is due.
POLL_SECONDS = 1.0


async def _authenticate(websocket: WebSocket, settings: Settings) -> bool:
    """Require the admin key as the first frame.

    Compared with ``hmac.compare_digest`` for the same reason as the HTTP admin
    dependency: a plain ``==`` on a secret leaks length and prefix information
    through timing.
    """
    try:
        raw = await asyncio.wait_for(
            websocket.receive_text(), timeout=AUTH_TIMEOUT_SECONDS
        )
    except (TimeoutError, WebSocketDisconnect):
        with contextlib.suppress(Exception):
            await websocket.close(code=POLICY_VIOLATION)
        return False

    key = ""
    with contextlib.suppress(json.JSONDecodeError, AttributeError, TypeError):
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            key = str(parsed.get("api_key") or "")

    # Encode before comparing: compare_digest rejects non-ASCII str inputs.
    if not key or not hmac.compare_digest(
        key.encode(), settings.admin_api_key.encode()
    ):
        logger.warning("dashboard_stream_rejected")
        with contextlib.suppress(Exception):
            await websocket.close(code=POLICY_VIOLATION)
        return False
    return True


@router.websocket("/ws/events")
async def dashboard_events(websocket: WebSocket) -> None:
    """Forward dashboard events to one authenticated operator."""
    settings = get_settings()
    await websocket.accept()
    if not await _authenticate(websocket, settings):
        return

    client = Redis.from_url(settings.redis_url)
    pubsub = client.pubsub()
    try:
        await pubsub.subscribe(CHANNEL)
        # Sent only once the subscription exists, so a client that has seen
        # "ready" knows it cannot miss events from this point on.
        await websocket.send_json({"type": "ready"})
        last_beat = time.monotonic()

        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=POLL_SECONDS
            )
            if message is not None:
                data = message.get("data")
                if isinstance(data, bytes | bytearray):
                    data = data.decode("utf-8", "replace")
                # Already JSON on the wire; forwarded verbatim rather than
                # decoded and re-encoded.
                await websocket.send_text(str(data))
                continue

            if time.monotonic() - last_beat >= HEARTBEAT_SECONDS:
                await websocket.send_json({"type": "heartbeat"})
                last_beat = time.monotonic()
    except WebSocketDisconnect:
        pass
    except Exception:
        # Most likely Redis went away. Closing tells the browser to reconnect,
        # and it falls back to polling meanwhile.
        logger.warning("dashboard_stream_failed", exc_info=True)
        with contextlib.suppress(Exception):
            await websocket.close(code=INTERNAL_ERROR)
    finally:
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(CHANNEL)
        with contextlib.suppress(Exception):
            await pubsub.aclose()
        with contextlib.suppress(Exception):
            await client.aclose()
