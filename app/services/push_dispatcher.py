"""Decides which dashboard events are worth a phone buzzing.

Sits between ``app/core/events.py`` and ``NotificationService``: the event bus
knows what happened, the notification service knows how to send, and the
mapping between the two lives here so that neither has to know about the
other's concerns.

Why hang push off the event bus at all, rather than calling the notification
service from the places where leads and handoffs are created? Because every
one of those places already publishes here. A sales lead, an AI handoff, an
operator assignment and an inbound message all reach ``publish()`` today, so
the four triggers in the specification need no changes to the message pipeline
-- and any future code path that announces a handoff gets push for free
instead of silently missing it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import ACTIVITY, HANDOFF
from app.core.logging import get_logger
from app.core.push_config import get_push_settings
from app.db.session import SessionLocal
from app.models.conversation import MODE_HUMAN, TAG_SALES_LEAD, Conversation
from app.models.user import User
from app.services.notification_service import (
    TYPE_ASSIGNED,
    TYPE_CUSTOMER_MESSAGE,
    TYPE_HANDOFF,
    TYPE_SALES_LEAD,
    NotificationService,
)

logger = get_logger(__name__)


async def _conversation_mode(session: AsyncSession, conversation_id: int) -> str | None:
    """Who is currently answering this conversation.

    Read from the row rather than taken from the event, because the activity
    event does not carry ``mode`` -- it carries only the conversation id and
    whether the turn was inbound. Widening that event was the alternative, and
    it would have meant editing every call site of ``conversation_activity()``
    and publishing a field that only push consumes.
    """
    return await session.scalar(
        select(Conversation.mode).where(Conversation.id == conversation_id)
    )


async def _customer_name(session: AsyncSession, conversation_id: int) -> str | None:
    """The customer's WhatsApp display name, for ``preview`` devices only.

    Fetched here and handed to the notification service, which decides per
    device whether it may be shown. Devices on the default ``private`` setting
    never see it, and no caller of this module can cause it to appear on a
    lock screen by accident.

    Never the phone number or the wa_id, both of which are on the same row and
    are deliberately not selected.
    """
    return await session.scalar(
        select(User.name)
        .join(Conversation, Conversation.user_id == User.id)
        .where(Conversation.id == conversation_id)
    )


async def _classify(
    session: AsyncSession, event: dict[str, Any], conversation_id: int
) -> str | None:
    """Which notification this event deserves, or None for silence.

    Silence is the default and every exclusion in the specification lands here:

    * outbound activity -- an AI reply or an operator's own reply. Nobody needs
      telling about a message they or their bot just sent.
    * a handoff back to ``bot`` mode -- the AI resuming is a lifecycle change,
      not a request for a human.
    * closed and reopened events -- internal session lifecycle.
    * read receipts, delivery receipts and typing indicators -- these never
      reach the event bus in the first place, so there is nothing to filter.
    """
    kind = event.get("type")

    if kind == HANDOFF:
        if event.get("mode") != MODE_HUMAN:
            return None
        # Order matters: a sales lead that arrives already assigned is still
        # a sales lead, and that is the more urgent thing to say.
        if event.get("tag") == TAG_SALES_LEAD:
            return TYPE_SALES_LEAD
        if event.get("assigned_operator"):
            return TYPE_ASSIGNED
        return TYPE_HANDOFF

    if kind == ACTIVITY:
        if not event.get("inbound"):
            return None
        # The one case that needs a database read: notify only while a person
        # owns the conversation. Every inbound message in bot mode is answered
        # by the AI within seconds and is not somebody's outstanding work.
        if await _conversation_mode(session, conversation_id) == MODE_HUMAN:
            return TYPE_CUSTOMER_MESSAGE
        return None

    return None


async def dispatch(event: dict[str, Any]) -> None:
    """Send push notifications for one dashboard event, if it warrants any.

    Never raises, and opens its own session. Called from ``events.publish()``,
    which runs in the Celery worker after the customer's reply is already
    committed -- there is no ambient transaction to join and nothing left to
    roll back, so a failure here must not propagate.

    Returns immediately when push is not configured, which is the normal state
    of a deployment that has not set up Firebase. That check is first so the
    common case costs nothing: no session, no query, no log line.
    """
    if not get_push_settings().configured:
        return

    conversation_id = event.get("conversation_id")
    if event.get("type") not in (ACTIVITY, HANDOFF):
        return
    if not isinstance(conversation_id, int):
        return

    try:
        async with SessionLocal() as session:
            notification_type = await _classify(session, event, conversation_id)
            if notification_type is None:
                return
            await NotificationService(session).notify(
                conversation_id=conversation_id,
                notification_type=notification_type,
                customer_name=await _customer_name(session, conversation_id),
            )
    except Exception as exc:
        # No conversation id in the message body beyond the id itself, and
        # never the customer's name -- a log line is not a lock screen, but it
        # is still a place customer data must not accumulate.
        logger.warning(
            "push_dispatch_failed",
            type=event.get("type"),
            conversation_id=conversation_id,
            error=str(exc),
        )
