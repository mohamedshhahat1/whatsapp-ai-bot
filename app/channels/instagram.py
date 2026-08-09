"""Instagram DM adapter.

Instagram Messaging is served by the Facebook Page linked to the Instagram
professional account, so the transport is the same Graph API host that the
Messenger adapter uses. That similarity is exactly where the danger is: the
differences are small, none of them raise, and every one of them would have
been wrong if it had been copied from ``messenger.py``.

* The webhook envelope's ``object`` is ``instagram``, not ``page``.
* The recipient is an Instagram-scoped id (IGSID), not a PSID.
* Text is capped at 1,000 BYTES, not 2,000 characters. Arabic is two bytes a
  letter in UTF-8, so a character-based clamp would send a body twice the
  documented limit and take a 400 on a real customer reply.
* The documented send path is ``/me/messages`` -- the token identifies the
  page. That is also why the registry requires ``instagram_account_id`` and
  not ``facebook_page_id``: the account id is the inbound echo guard, not part
  of the outbound URL.
* Echoes arrive on the ``messages`` subscription itself rather than on a
  separate ``message_echoes`` field, flagged ``is_echo``. Messages the account
  sent to itself are flagged ``is_self``.

Nothing here was inferred from the Messenger payload shape. Each fact above is
cited in docs/CHANNELS.md against Meta's own reference and pinned by a test in
tests/test_instagram.py, so a later reader can tell what was verified from
what was assumed.

Sources:
  https://developers.facebook.com/documentation/business-messaging/instagram-messaging/webhooks
  https://developers.facebook.com/documentation/business-messaging/instagram-messaging/features/send-message
  https://developers.facebook.com/documentation/business-messaging/instagram-messaging/features/quick-replies
  https://developers.facebook.com/documentation/instagram-platform/self-messaging
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import httpx

from app.channels.base import BaseChannelAdapter
from app.channels.config import ChannelSettings
from app.channels.constants import INSTAGRAM_DM
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

#: The endpoint Meta documents for Instagram sends. ``me`` resolves to the page
#: the access token belongs to, so no id is interpolated into the path -- which
#: is what lets this channel work with the credentials the registry asks for.
SEND_PATH = "/me/messages"

#: Instagram states this limit in BYTES, unlike Messenger's character count.
#: See the module docstring for why that distinction is not cosmetic here.
TEXT_MAX_BYTES = 1000

#: "A maximum of 13 quick replies are supported and each quick reply allows up
#: to 20 characters before being truncated."
QUICK_REPLY_TITLE_MAX = 20
MAX_QUICK_REPLIES = 13


def clip_utf8(text: str, max_bytes: int = TEXT_MAX_BYTES) -> str:
    """Trim ``text`` to at most ``max_bytes`` UTF-8 bytes.

    Slicing encoded bytes can land in the middle of a multi-byte character, so
    the decode drops a trailing partial one rather than raising. Public because
    the suite asserts on it directly: an Arabic reply is the case that makes a
    character-based clamp wrong, and it needs to stay wrong-proof.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", "ignore")


@register_adapter
class InstagramDMAdapter(BaseChannelAdapter):
    """Thin async client over the Instagram Send API, plus payload parsing."""

    channel = INSTAGRAM_DM

    def __init__(self, settings: ChannelSettings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=GRAPH_API_BASE + "/" + settings.meta_api_version,
            # instagram_token() falls back to the page token, which is the
            # normal setup: an IG professional account behind a page is served
            # by that page's token.
            headers={"Authorization": "Bearer " + settings.instagram_token()},
            timeout=30.0,
        )

    # --- Outbound -----------------------------------------------------------

    @http_retry()
    async def _send(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Single attempt; tenacity retries transient failures (429/5xx/network)."""
        response = await self._client.post(SEND_PATH, json=payload)
        response.raise_for_status()
        return response.json()

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send with retries, translating exhausted failures to a domain error.

        Raises ``ExternalServiceError`` for the reason base.py sets out: upstream
        ``_send_once`` tells a send that failed from one that never happened, and
        it can only do that if every adapter fails the same way.
        """
        try:
            return await self._send(payload)
        except httpx.HTTPStatusError as exc:
            logger.error(
                "instagram_api_error",
                status_code=exc.response.status_code,
                body=exc.response.text[:500],
            )
            raise ExternalServiceError("Instagram API request failed") from exc
        except httpx.HTTPError as exc:
            logger.error("instagram_network_error", error=str(exc))
            raise ExternalServiceError("Instagram API unreachable") from exc

    async def send_text(self, recipient: str, text: str) -> dict[str, Any]:
        """Send a plain text message to an IGSID.

        ``messaging_type: RESPONSE`` states this is a reply to something the
        customer sent, which keeps it inside the 24-hour window without a tag.
        Meta's Instagram samples include the field, so it is not Messenger-only.
        """
        return await self._post(
            {
                "recipient": {"id": recipient},
                "messaging_type": "RESPONSE",
                "message": {"text": clip_utf8(text)},
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
        the payload and comes back verbatim on a tap, so callers route on it and
        never on the title -- see app/services/menu.py.
        """
        if not options:
            return await self.send_text(recipient, text)
        return await self._post(
            {
                "recipient": {"id": recipient},
                "messaging_type": "RESPONSE",
                "message": {
                    "text": clip_utf8(text),
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
        """Turn one Instagram webhook delivery into normalised events.

        ``entry[].id`` is the app user's own Instagram professional account id,
        which is what makes the echo check below possible without configuration.
        One delivery can carry several entries, each with several messaging
        items, from several people.
        """
        events: list[InboundEvent] = []
        for entry in payload.get("entry") or []:
            account_id = str(entry.get("id") or "")
            for item in entry.get("messaging") or []:
                event = self._parse_item(item, account_id)
                if event is not None and event.routable:
                    events.append(event)
        return events

    def _parse_item(
        self,
        item: dict[str, Any],
        account_id: str,
    ) -> InboundEvent | None:
        sender_id = str((item.get("sender") or {}).get("id") or "")
        message = item.get("message") or {}

        # Echoes first, before anything else reads the payload. Instagram
        # delivers them on the `messages` subscription rather than on a
        # separate field, so without this the bot answers itself and the loop
        # costs a completion per turn until somebody notices.
        #
        # Three independent guards because they cover different cases: is_echo
        # is the documented flag, is_self covers a message the account sent to
        # itself, and the id comparison catches an echo whose flag is absent.
        if message.get("is_echo") or message.get("is_self"):
            return None
        configured = self._settings.instagram_account_id.strip()
        if sender_id and sender_id in {account_id, configured} - {""}:
            return None

        # An unsend. There is nothing to answer, and treating it as text would
        # reply to a message the customer has just withdrawn.
        if message.get("is_deleted"):
            return None

        postback = item.get("postback") or {}
        if not message and not postback:
            # Read receipts and reactions share this array and carry no
            # message. Falling through to "unsupported" would apologise to a
            # customer who did nothing.
            return None

        sent_at = self._timestamp(item.get("timestamp"))

        if postback:
            return InboundEvent(
                channel=INSTAGRAM_DM,
                sender_id=sender_id,
                provider_message_id=str(postback.get("mid") or ""),
                kind=EVENT_SELECTION,
                selection_id=str(postback.get("payload") or ""),
                selection_title=postback.get("title"),
                sent_at=sent_at,
            )

        mid = str(message.get("mid") or "")

        # Something this Graph API version cannot represent. Explicitly
        # unsupported rather than empty text, so the customer gets the
        # "cannot read this" reply instead of silence.
        if message.get("is_unsupported"):
            return InboundEvent(
                channel=INSTAGRAM_DM,
                sender_id=sender_id,
                provider_message_id=mid,
                kind=EVENT_UNSUPPORTED,
                sent_at=sent_at,
            )

        # A tapped quick reply arrives with the visible label in `text` and the
        # routing id in the payload. Reading the label would route on
        # human-readable copy, which menu.py exists to prevent.
        quick_reply = message.get("quick_reply") or {}
        if quick_reply:
            return InboundEvent(
                channel=INSTAGRAM_DM,
                sender_id=sender_id,
                provider_message_id=mid,
                kind=EVENT_SELECTION,
                selection_id=str(quick_reply.get("payload") or ""),
                selection_title=message.get("text"),
                sent_at=sent_at,
            )

        # Instagram carries story mentions, shared posts and reels here
        # alongside ordinary images. All of them are media as far as the shared
        # pipeline is concerned; the type is preserved for the operator view.
        attachments = message.get("attachments") or []
        if attachments:
            first = attachments[0] or {}
            return InboundEvent(
                channel=INSTAGRAM_DM,
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
                channel=INSTAGRAM_DM,
                sender_id=sender_id,
                provider_message_id=mid,
                kind=EVENT_TEXT,
                text=text,
                sent_at=sent_at,
            )

        return InboundEvent(
            channel=INSTAGRAM_DM,
            sender_id=sender_id,
            provider_message_id=mid,
            kind=EVENT_UNSUPPORTED,
            sent_at=sent_at,
        )

    @staticmethod
    def _timestamp(value: Any) -> datetime | None:
        """Instagram sends epoch milliseconds, as Messenger does.

        Returns None rather than raising on anything unreadable: the freshness
        gate fails open, and a format change on Meta's side must not silence
        every reply the bot makes.
        """
        try:
            return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
        except (TypeError, ValueError):
            logger.warning("instagram_timestamp_unparseable", value=str(value))
            return None
