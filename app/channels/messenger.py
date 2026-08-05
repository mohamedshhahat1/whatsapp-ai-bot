"""Facebook Messenger adapter.

Mirrors WhatsAppClient's structure on purpose -- same retry decorator, same
_send/_post split, same domain error on the way out. ``_send_once`` upstream
tells a send that failed apart from one that never happened, and it can only
do that if every adapter raises ExternalServiceError the same way.

The outbound half is small. The inbound half is where Messenger differs from
WhatsApp in ways that bite: echoes of the page's own messages, delivery and
read receipts sharing the same array as real messages, and quick replies that
carry their routing id in a payload beside the visible text.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import httpx

from app.channels.base import BaseChannelAdapter
from app.channels.config import ChannelSettings
from app.channels.constants import MESSENGER
from app.channels.events import (
    EVENT_MEDIA,
    EVENT_SELECTION,
    EVENT_TEXT,
    EVENT_UNSUPPORTED,
    InboundEvent,
)
from app.channels.registry import register_adapter
from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger
from app.core.retry import http_retry

logger = get_logger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com"

# Messenger's own limits. Exceeding one is a 400 from the Graph API, which
# would surface as a failed customer reply, so they are clamped here rather
# than trusted to every caller.
TEXT_MAX = 2000
QUICK_REPLY_TITLE_MAX = 20
MAX_QUICK_REPLIES = 13


@register_adapter
class MessengerAdapter(BaseChannelAdapter):
    """Thin async client over the Messenger Send API, plus payload parsing."""

    channel = MESSENGER

    def __init__(self, settings: ChannelSettings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=GRAPH_API_BASE + "/" + settings.meta_api_version,
            headers={
                "Authorization": "Bearer " + settings.facebook_page_access_token
            },
            timeout=30.0,
        )

    # --- Outbound -----------------------------------------------------------

    @http_retry()
    async def _send(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Single attempt; tenacity retries transient failures (429/5xx/network)."""
        url = "/" + self._settings.facebook_page_id + "/messages"
        response = await self._client.post(url, json=payload)
        response.raise_for_status()
        return response.json()

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send with retries, translating exhausted failures to a domain error."""
        try:
            return await self._send(payload)
        except httpx.HTTPStatusError as exc:
            logger.error(
                "messenger_api_error",
                status_code=exc.response.status_code,
                body=exc.response.text[:500],
            )
            raise ExternalServiceError("Messenger API request failed") from exc
        except httpx.HTTPError as exc:
            logger.error("messenger_network_error", error=str(exc))
            raise ExternalServiceError("Messenger API unreachable") from exc

    async def send_text(self, recipient: str, text: str) -> dict[str, Any]:
        """Send a plain text message.

        ``messaging_type: RESPONSE`` states this is a reply to something the
        customer sent, which is what keeps it inside the 24-hour window
        without needing a message tag.
        """
        return await self._post(
            {
                "recipient": {"id": recipient},
                "messaging_type": "RESPONSE",
                "message": {"text": text[:TEXT_MAX]},
            }
        )

    async def send_quick_replies(
        self,
        recipient: str,
        text: str,
        options: list[tuple[str, str]],
    ) -> dict[str, Any]:
        """Send text with tappable quick replies.

        ``options`` is a list of ``(selection_id, title)``. The id travels as
        the payload and comes back verbatim on a tap, so callers route on it
        and never on the title -- see app/services/menu.py.
        """
        if not options:
            return await self.send_text(recipient, text)
        return await self._post(
            {
                "recipient": {"id": recipient},
                "messaging_type": "RESPONSE",
                "message": {
                    "text": text[:TEXT_MAX],
                    "quick_replies": [
                        {
                            "content_type": "text",
                            "title": title[:QUICK_REPLY_TITLE_MAX],
                            "payload": selection_id,
                        }
                        for selection_id, title in options[:MAX_QUICK_REPLIES]
                    ],
                },
            }
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- Inbound ------------------------------------------------------------

    def parse(self, payload: dict[str, Any]) -> Iterable[InboundEvent]:
        """Turn one Messenger webhook delivery into normalised events.

        One delivery can carry several entries, each with several messaging
        items, from several people.
        """
        events: list[InboundEvent] = []
        for entry in payload.get("entry") or []:
            page_id = str(entry.get("id") or "")
            for item in entry.get("messaging") or []:
                event = self._parse_item(item, page_id)
                if event is not None and event.routable:
                    events.append(event)
        return events

    def _parse_item(
        self, item: dict[str, Any], page_id: str
    ) -> InboundEvent | None:
        sender_id = str((item.get("sender") or {}).get("id") or "")
        message = item.get("message") or {}

        # Echoes first, before anything else reads the payload. The page's own
        # outgoing messages come back as webhooks; answering one produces a
        # reply that also echoes, and the loop costs an OpenAI completion per
        # turn until somebody notices.
        if message.get("is_echo") or (page_id and sender_id == page_id):
            return None

        postback = item.get("postback") or {}
        if not message and not postback:
            # Delivery and read receipts share this array and carry no
            # message. Falling through to "unsupported" would apologise to a
            # customer who did nothing.
            return None

        sent_at = self._timestamp(item.get("timestamp"))

        # A postback is a tapped persistent-menu or get-started button.
        if postback:
            return InboundEvent(
                channel=MESSENGER,
                sender_id=sender_id,
                provider_message_id=str(postback.get("mid") or ""),
                kind=EVENT_SELECTION,
                selection_id=str(postback.get("payload") or ""),
                selection_title=postback.get("title"),
                sent_at=sent_at,
            )

        mid = str(message.get("mid") or "")

        # A tapped quick reply arrives with the visible label in `text` and
        # the routing id in the payload. Reading the label would route on
        # human-readable copy, which menu.py exists to prevent.
        quick_reply = message.get("quick_reply") or {}
        if quick_reply:
            return InboundEvent(
                channel=MESSENGER,
                sender_id=sender_id,
                provider_message_id=mid,
                kind=EVENT_SELECTION,
                selection_id=str(quick_reply.get("payload") or ""),
                selection_title=message.get("text"),
                sent_at=sent_at,
            )

        attachments = message.get("attachments") or []
        if attachments:
            first = attachments[0] or {}
            return InboundEvent(
                channel=MESSENGER,
                sender_id=sender_id,
                provider_message_id=mid,
                kind=EVENT_MEDIA,
                media_type=first.get("type"),
                media_url=(first.get("payload") or {}).get("url"),
                caption=message.get("text"),
                sent_at=sent_at,
            )

        text = message.get("text")
        if text:
            return InboundEvent(
                channel=MESSENGER,
                sender_id=sender_id,
                provider_message_id=mid,
                kind=EVENT_TEXT,
                text=text,
                sent_at=sent_at,
            )

        return InboundEvent(
            channel=MESSENGER,
            sender_id=sender_id,
            provider_message_id=mid,
            kind=EVENT_UNSUPPORTED,
            sent_at=sent_at,
        )

    @staticmethod
    def _timestamp(value: Any) -> datetime | None:
        """Messenger sends epoch milliseconds.

        Returns None rather than raising on anything unreadable: the freshness
        gate fails open, and a format change on Meta's side must not silence
        every reply the bot makes.
        """
        try:
            return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
        except (TypeError, ValueError):
            logger.warning("messenger_timestamp_unparseable", value=str(value))
            return None
