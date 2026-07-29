"""Realtime dashboard events, fanned out over Redis pub/sub.

Inbound messages are processed by the Celery worker, which is a different
process from the API that holds the operator's WebSocket -- and in production
there may be several API replicas behind nginx. A worker therefore cannot write
to the socket directly. It publishes here instead, and every API replica
forwards what it receives to the dashboards connected to it.

Events are deliberately thin: they say *that* a conversation changed, not
*what* was said. The dashboard refetches through the authenticated admin API,
so there is still one source of truth, and no customer phone number, name or
message body is ever written to the message bus.
"""

import json
from datetime import UTC, datetime
from typing import Any

from app.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Single channel: the volume is a handful of events per customer message, and
# per-conversation channels would mean resubscribing every time an operator
# clicks a different row.
CHANNEL = "dashboard:events"

ACTIVITY = "conversation.activity"


def conversation_activity(*, conversation_id: int, inbound: bool) -> dict[str, Any]:
    """Build the event announcing that a conversation has new messages.

    ``inbound`` marks a turn that started with the customer. The dashboard uses
    it to decide whether to pull the operator's attention to the conversation:
    the bot answering itself, or an operator's own manual reply, should not.
    """
    return {
        "type": ACTIVITY,
        "conversation_id": conversation_id,
        "inbound": inbound,
        "at": datetime.now(UTC).isoformat(),
    }


async def publish(event: dict[str, Any], settings: Settings | None = None) -> None:
    """Fan one event out to every connected dashboard.

    A short-lived client per publish, on purpose. The Celery worker runs each
    task in its own event loop, and a cached asyncio Redis client would still
    hold connections bound to a loop that has already closed. One connection
    per event is affordable at a few events per customer message; a shared
    client would be a latent cross-loop bug.

    Failures are logged and swallowed. A dashboard that misses a refresh hint
    falls back to polling; a customer whose reply was not sent because the
    notification bus was down would be a real outage. The reply has already
    been committed by the time this runs.
    """
    settings = settings or get_settings()
    try:
        from redis.asyncio import Redis

        client = Redis.from_url(settings.redis_url)
        try:
            await client.publish(CHANNEL, json.dumps(event))
        finally:
            await client.aclose()
    except Exception as exc:
        logger.warning(
            "event_publish_failed", type=event.get("type"), error=str(exc)
        )
