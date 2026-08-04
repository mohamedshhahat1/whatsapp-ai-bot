"""Webhook payload processing shared by the Celery worker and inline fallback.

Exceptions are allowed to propagate so the task queue can retry the delivery.
Message deduplication (by ``wa_message_id``) makes retries safe: already
processed messages are skipped on the next attempt.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.logging import get_logger
from app.core.metrics import ERRORS_TOTAL
from app.integrations.openai import OpenAIClient
from app.integrations.whatsapp import WhatsAppClient
from app.services.chat_service import ChatService

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
                await _dispatch_message(service, message, contacts)
            for status in value.get("statuses", []):
                wa_message_id = status.get("id")
                new_status = status.get("status")
                if wa_message_id and new_status:
                    await service.handle_status_update(wa_message_id, new_status)


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
