"""Webhook payload processing shared by the Celery worker and inline fallback.

Exceptions are allowed to propagate so the task queue can retry the delivery.
Message deduplication (by ``wa_message_id``) makes retries safe: already
processed messages are skipped on the next attempt.

Freshness
---------
Every message is checked against ``INBOUND_MAX_AGE_MINUTES`` before it is
routed, and a stale one is recorded without a reply. This is the one place
that check can live: it is the single point every inbound message of every
type passes through, so no handler can be added later that forgets it.

It is here rather than deeper down because staleness is a property of the
DELIVERY, not of the conversation. By the time ``ChatService`` has a
conversation in hand the damage is already done -- ``get_context`` has decided
whether to resume a session or mint a new one, and a new one is owed a
welcome. The decision has to be made before that, on the payload itself.

Two entry points
----------------
``process_webhook_payload`` handles WhatsApp Cloud API deliveries and parses
their payload inline. ``process_meta_payload`` handles the other Meta channels
and parses nothing: the adapter has already turned the delivery into
``InboundEvent`` objects, so all that is left is routing them.

They are kept separate deliberately. Folding WhatsApp into the event model
would mean rewriting a parser that is in production and correct, to satisfy a
shape it never receives -- and the whole point of the channel work is that the
live WhatsApp path does not move.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import BaseChannelAdapter
from app.channels.events import (
    EVENT_MEDIA,
    EVENT_SELECTION,
    EVENT_TEXT,
    InboundEvent,
)
from app.config import Settings
from app.core.inbound_config import get_inbound_settings
from app.core.logging import get_logger
from app.core.metrics import ERRORS_TOTAL
from app.integrations.openai import OpenAIClient
from app.integrations.whatsapp import WhatsAppClient
from app.services.chat_service import ChatService
from app.services.stale_inbound import record_without_answering

logger = get_logger(__name__)


async def process_webhook_payload(
    session: AsyncSession,
    whatsapp: WhatsAppClient,
    ai: OpenAIClient,
    settings: Settings,
    payload: dict[str, Any],
) -> None:
    """Process all messages and status updates in one webhook delivery."""
    service = ChatService(session, whatsapp, ai, settings)
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            contacts = {
                contact.get("wa_id"): contact.get("profile", {}).get("name")
                for contact in value.get("contacts", [])
            }
            for message in value.get("messages", []):
                if await _handled_as_stale(session, settings, message, contacts):
                    continue
                await _dispatch_message(service, message, contacts)
            for status in value.get("statuses", []):
                wa_message_id = status.get("id")
                new_status = status.get("status")
                if wa_message_id and new_status:
                    await service.handle_status_update(wa_message_id, new_status)


async def process_meta_payload(
    session: AsyncSession,
    adapter: BaseChannelAdapter,
    ai: OpenAIClient,
    settings: Settings,
    payload: dict[str, Any],
) -> None:
    """Process one delivery from a non-WhatsApp Meta channel.

    The adapter owns everything channel-shaped: it drops echoes and receipts,
    pulls the routing id out of a quick reply, and reports the timestamp in
    whatever unit its API uses. What arrives here is already normalised, so
    this function only decides which handler each event belongs to.

    The service is constructed with the adapter as its sender and told which
    channel it is on, which is what makes replies leave through the same
    channel they arrived on.
    """
    service = ChatService(
        session, adapter, ai, settings, channel=adapter.channel
    )
    for event in adapter.parse(payload):
        if _event_is_stale(event):
            continue
        await _dispatch_event(service, event)


def _message_age(message: dict[str, Any]) -> timedelta | None:
    """How long ago the customer sent this message, per Meta's own timestamp.

    ``None`` when the field is absent or unparseable, which callers treat as
    fresh. That is a deliberate fail-open: a format change on Meta's side
    would otherwise silence every reply the bot makes, which is a far worse
    failure than the one this check exists to prevent.
    """
    raw = message.get("timestamp")
    if raw is None:
        return None
    try:
        sent_at = datetime.fromtimestamp(int(raw), tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        logger.warning("inbound_timestamp_unparseable", timestamp=str(raw))
        return None
    return datetime.now(UTC) - sent_at


def _event_is_stale(event: InboundEvent) -> bool:
    """Whether a normalised event arrived too late to be worth answering.

    Same policy and same setting as the WhatsApp path, and the same fail-open
    behaviour: ``event.age`` is ``None`` when the timestamp was unreadable,
    and that counts as fresh.

    Unlike the WhatsApp path this does NOT record the message. The recorder
    resolves a customer by ``wa_id``, and handing it a page-scoped id would
    write a phone-shaped user row and undo the identity separation the channel
    work is built on. The outcome for the customer is identical either way --
    a stale message is never answered -- so what is lost is the transcript
    entry, not a reply. Worth fixing when the recorder becomes channel-aware.
    """
    inbound = get_inbound_settings()
    if not inbound.enforced:
        return False

    age = event.age
    if age is None or age <= inbound.inbound_max_age:
        return False

    logger.info(
        "stale_inbound_skipped",
        channel=event.channel,
        provider_message_id=event.provider_message_id,
        age_seconds=int(age.total_seconds()),
    )
    return True


def _content_of(message: dict[str, Any]) -> str | None:
    """What to store in the transcript for a message that is not answered.

    Mirrors what each handler in ``ChatService`` would have stored, so a stale
    delivery reads the same as a live one to an operator.
    """
    message_type = message.get("type", "unknown")
    if message_type == "text":
        return message.get("text", {}).get("body")
    if message_type in ("image", "document"):
        media = message.get(message_type, {})
        caption = media.get("caption") if isinstance(media, dict) else None
        return caption or f"[{message_type} received]"
    if message_type == "interactive":
        selection = _interactive_selection(message)
        if selection is None:
            return "[interactive received]"
        selection_id, title = selection
        return title or selection_id
    if message_type == "button":
        button = message.get("button", {})
        if isinstance(button, dict):
            return str(button.get("text") or button.get("payload") or "")
    return f"[{message_type} received]"


async def _handled_as_stale(
    session: AsyncSession,
    settings: Settings,
    message: dict[str, Any],
    contacts: dict[str, str | None],
) -> bool:
    """Record and swallow a delivery that arrived too late to answer.

    ``True`` means this message has been dealt with and must not be routed to
    a handler. ``False`` means it is live traffic.
    """
    inbound = get_inbound_settings()
    if not inbound.enforced:
        return False

    age = _message_age(message)
    if age is None or age <= inbound.inbound_max_age:
        return False

    wa_id = message.get("from", "")
    wa_message_id = message.get("id", "")
    if not wa_id or not wa_message_id:
        # Nothing to key the claim on. Let the normal path handle it and log
        # whatever it finds rather than dropping it here.
        return False

    await record_without_answering(
        session,
        settings,
        wa_id=wa_id,
        name=contacts.get(wa_id),
        wa_message_id=wa_message_id,
        type=message.get("type", "unknown"),
        content=_content_of(message),
        age=age,
    )
    return True


def _interactive_selection(message: dict[str, Any]) -> tuple[str, str] | None:
    """Extract ``(selection_id, title)`` from an interactive reply.

    Meta uses a different envelope per menu type -- ``button_reply`` for reply
    buttons, ``list_reply`` for list rows -- carrying the same two fields. The
    id is the one that matters; see app/services/menu.py for why the title is
    never routed on.

    ``None`` when the envelope is missing or carries no id, which is not worth
    guessing at: without an id there is nothing to route.
    """
    interactive = message.get("interactive", {})
    if not isinstance(interactive, dict):
        return None
    for key in ("button_reply", "list_reply"):
        reply = interactive.get(key)
        if isinstance(reply, dict) and reply.get("id"):
            return str(reply["id"]), str(reply.get("title") or "")
    return None


async def _dispatch_event(service: ChatService, event: InboundEvent) -> None:
    """Route one normalised event to the right handler.

    The provider message id is the idempotency key for the whole pipeline --
    the inbound claim, the generation cache and the outbound reservation all
    hang off it -- so an event without one is dropped rather than processed
    under an empty string, which every other message would then collide with.

    Errors are counted and re-raised exactly as on the WhatsApp path, so the
    queue retries the delivery.
    """
    if not event.sender_id or not event.provider_message_id:
        logger.warning(
            "unroutable_event_skipped",
            channel=event.channel,
            kind=event.kind,
            has_sender=bool(event.sender_id),
        )
        return

    try:
        if event.kind == EVENT_TEXT:
            await service.handle_text_message(
                event.sender_id,
                event.sender_name,
                event.provider_message_id,
                event.text or "",
            )
        elif event.kind == EVENT_SELECTION:
            if event.selection_id:
                await service.handle_interactive_message(
                    event.sender_id,
                    event.sender_name,
                    event.provider_message_id,
                    event.selection_id,
                    event.selection_title or "",
                )
            else:
                # A tap we cannot route: the payload was empty. Treated as
                # unsupported rather than guessed at from the visible label.
                await service.handle_unsupported_message(
                    event.sender_id,
                    event.sender_name,
                    event.provider_message_id,
                    "interactive",
                )
        elif event.kind == EVENT_MEDIA:
            # media_id stays None on Messenger: that column holds a Cloud API
            # media id, and Messenger supplies a short-lived signed CDN URL
            # instead. The customer is still told their attachment arrived.
            await service.handle_media_message(
                event.sender_id,
                event.sender_name,
                event.provider_message_id,
                event.media_type or "media",
                event.media_id,
                event.caption,
            )
        else:
            await service.handle_unsupported_message(
                event.sender_id,
                event.sender_name,
                event.provider_message_id,
                event.kind,
            )
    except Exception:
        ERRORS_TOTAL.labels(type="message_processing").inc()
        logger.error(
            "message_processing_failed",
            wa_message_id=event.provider_message_id,
            type=event.kind,
            channel=event.channel,
            exc_info=True,
        )
        raise  # propagate so the queue can retry the delivery


async def _dispatch_message(
    service: ChatService, message: dict[str, Any], contacts: dict[str, str | None]
) -> None:
    """Route a single inbound message to the right handler."""
    wa_id = message.get("from", "")
    wa_message_id = message.get("id", "")
    message_type = message.get("type", "unknown")
    name = contacts.get(wa_id)
    try:
        if message_type == "text":
            await service.handle_text_message(
                wa_id, name, wa_message_id, message.get("text", {}).get("body", "")
            )
        elif message_type in ("image", "document"):
            media = message.get(message_type, {})
            await service.handle_media_message(
                wa_id,
                name,
                wa_message_id,
                message_type,
                media.get("id"),
                media.get("caption"),
            )
        elif message_type == "interactive":
            selection = _interactive_selection(message)
            if selection is None:
                await service.handle_unsupported_message(
                    wa_id, name, wa_message_id, message_type
                )
            else:
                selection_id, title = selection
                await service.handle_interactive_message(
                    wa_id, name, wa_message_id, selection_id, title
                )
        elif message_type == "button":
            # A quick-reply button on a template message. Different envelope,
            # same meaning: ``payload`` is the stable id we attached when the
            # template was created, ``text`` is the label the customer saw.
            button = message.get("button", {})
            payload = button.get("payload") if isinstance(button, dict) else None
            if payload:
                await service.handle_interactive_message(
                    wa_id,
                    name,
                    wa_message_id,
                    str(payload),
                    str(button.get("text") or ""),
                )
            else:
                await service.handle_unsupported_message(
                    wa_id, name, wa_message_id, message_type
                )
        else:
            await service.handle_unsupported_message(
                wa_id, name, wa_message_id, message_type
            )
    except Exception:
        ERRORS_TOTAL.labels(type="message_processing").inc()
        logger.error(
            "message_processing_failed",
            wa_message_id=wa_message_id,
            type=message_type,
            exc_info=True,
        )
        raise  # propagate so the queue can retry the delivery
