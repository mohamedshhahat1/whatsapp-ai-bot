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

The lifecycle events below bend that rule in one narrow way: they carry the
conversation's own lifecycle columns (status and its timestamps). Those are
not customer data -- they are facts about the row, not about the person -- and
carrying them means a dashboard can repaint a status badge without a round
trip. Message *content* still never appears here.

This module is also where mobile push notifications are triggered, for the
reason given in ``app/services/push_dispatcher.py``: every code path that a
dashboard needs to hear about already reports here, so a phone can listen to
the same place instead of the message pipeline growing a second set of hooks.
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
HANDOFF = "conversation.handoff"
CLOSED = "conversation.closed"
REOPENED = "conversation.reopened"


def _moment(value: datetime | None) -> str | None:
    """ISO-format a timestamp for the bus, tolerating NULL columns.

    ``closed_at`` and friends are nullable by design, and a client that
    receives ``null`` learns something true. Formatting here rather than at
    each call site keeps every event's timestamps in one shape.
    """
    return value.isoformat() if value is not None else None


def conversation_activity(*, conversation_id: int, inbound: bool) -> dict[str, Any]:
    """Build the event announcing that a conversation has new messages.

    ``inbound`` marks a turn that started with the customer. The dashboard uses
    it to decide whether to pull the operator's attention to the conversation:
    the bot answering itself, or an operator's own manual reply, should not.
    Push notifications use it for the same purpose and more strictly -- see
    ``push_dispatcher._classify``.
    """
    return {
        "type": ACTIVITY,
        "conversation_id": conversation_id,
        "inbound": inbound,
        "at": datetime.now(UTC).isoformat(),
    }


def conversation_handoff(
    *,
    conversation_id: int,
    mode: str,
    assigned_operator: str | None,
    reason: str,
    tag: str | None = None,
) -> dict[str, Any]:
    """Build the event announcing that ownership of a conversation changed.

    Separate from the activity event rather than a field on it, so that a
    dashboard showing the conversation can distinguish "a message arrived"
    from "the bot has stopped answering this".

    ``assigned_operator`` is a staff name, not customer data. It is on the bus
    because a second operator's screen has to show who already owns the
    conversation; without it, two people answer the same customer.

    ``tag`` is here so a dashboard can alert differently for a sales lead
    without a round trip. It is a fixed vocabulary word, not customer data --
    the rule that no phone number, name or message body goes on the bus still
    holds. A dashboard that has not been taught the tag ignores an unknown
    string and shows an ordinary handoff, which is the correct degradation.
    """
    return {
        "type": HANDOFF,
        "conversation_id": conversation_id,
        "mode": mode,
        "assigned_operator": assigned_operator,
        "reason": reason,
        "tag": tag,
        "at": datetime.now(UTC).isoformat(),
    }


def conversation_closed(
    *,
    conversation_id: int,
    user_id: int,
    status: str,
    closed_at: datetime | None,
    updated_at: datetime | None,
) -> dict[str, Any]:
    """Build the event announcing that a session has ended.

    Published for EVERY close, not only the ones that sent a goodbye. That
    distinction is the whole reason this event exists: the sweeper closes
    silently whenever the closing message is disabled, the copy is empty, or
    the conversation has fallen outside Meta's service window, and in those
    cases the old activity event never fired. A dashboard would keep showing
    "active" indefinitely, because the conversation list stops polling while
    the event stream is connected -- so the stale row never self-corrected on
    a *healthy* system.

    Carries the resulting state rather than a bare id so a client can repaint
    the row immediately and still refetch at its leisure.
    """
    return {
        "type": CLOSED,
        "conversation_id": conversation_id,
        "user_id": user_id,
        "status": status,
        "closed_at": _moment(closed_at),
        "updated_at": _moment(updated_at),
        "at": datetime.now(UTC).isoformat(),
    }


def conversation_reopened(
    *,
    conversation_id: int,
    user_id: int,
    status: str,
    reason: str,
    updated_at: datetime | None,
) -> dict[str, Any]:
    """Build the event announcing that a closed session was revived.

    ``reason`` distinguishes the two ways that happens, because they mean
    different things to a watching operator:

    * ``"customer"`` -- the customer came back inside the reopen window and
      the conversation resumed by itself.
    * ``"operator"`` -- somebody replied to, or took over, a closed
      conversation, and it was revived so the reply had somewhere to live.

    A row that reappears as active with no explanation looks like a bug, and
    an operator who cannot tell "the customer is back" from "my colleague
    just claimed this" will act on the wrong one.
    """
    return {
        "type": REOPENED,
        "conversation_id": conversation_id,
        "user_id": user_id,
        "status": status,
        "reason": reason,
        "updated_at": _moment(updated_at),
        "at": datetime.now(UTC).isoformat(),
    }


async def _notify_devices(event: dict[str, Any]) -> None:
    """Offer one event to the mobile push dispatcher.

    Imported inside the function, like the Redis client above, so that neither
    ``app.core`` nor the test suite acquires a dependency on the service layer
    at import time.

    Wrapped in its own try/except rather than sharing the publish one. The two
    failures are unrelated: a dashboard that missed a refresh hint because
    Redis is down should not also lose its phone notification, and Firebase
    being unreachable must not stop the WebSocket event that is already out.
    """
    try:
        from app.services.push_dispatcher import dispatch

        await dispatch(event)
    except Exception as exc:
        logger.warning(
            "push_dispatch_unavailable", type=event.get("type"), error=str(exc)
        )


async def publish(event: dict[str, Any], settings: Settings | None = None) -> None:
    """Fan one event out to every connected dashboard, and to mobile devices.

    A short-lived client per publish, on purpose. The Celery worker runs each
    task in its own event loop, and a cached asyncio Redis client would still
    hold connections bound to a loop that has already closed. One connection
    per event is affordable at a few events per customer message; a shared
    client would be a latent cross-loop bug.

    Failures are logged and swallowed. A dashboard that misses a refresh hint
    falls back to polling; a customer whose reply was not sent because the
    notification bus was down would be a real outage. The reply has already
    been committed by the time this runs.

    Push is attempted after the publish and independently of whether it
    succeeded, because the two audiences are unrelated: an operator sitting in
    front of the dashboard and one whose phone is in a pocket should not share
    a single point of failure.
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
        logger.warning("event_publish_failed", type=event.get("type"), error=str(exc))

    await _notify_devices(event)
