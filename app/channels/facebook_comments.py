"""Facebook Page comments: answer in the open, continue in private.

A comment is not a message, and most of this adapter is the consequence of
that. It arrives on a different part of the envelope, it carries a different
timestamp unit, it has no session, and the reply is addressed to the comment
rather than to the person who wrote it.

Verified provider contract
--------------------------
Every shape below was read from Meta's documentation, not inferred from the
Messenger adapter:

- Page webhook field reference (``feed``, ``item``, ``verb``, ``comment_id``,
  ``created_time``):
  https://developers.facebook.com/docs/graph-api/webhooks/reference/page/
- Public replies (the ``/comments`` edge):
  https://developers.facebook.com/docs/graph-api/reference/object/comments
- Private replies (``recipient: {comment_id}``, one per commenter, 7 days):
  https://developers.facebook.com/documentation/business-messaging/messenger-platform/discovery/private-replies

Two traps worth naming, because both are silent
-----------------------------------------------
``created_time`` on the ``feed`` field is epoch **seconds**. Every messaging
surface in this repository sends milliseconds, so reusing one of those
``/1000`` helpers dates every comment to January 1970 -- at which point the
inbound freshness gate discards all of them and the bot simply never answers
a comment. ``_timestamp`` here does not divide, and a test pins it.

``field == "feed"`` is not "a comment happened". The same field reports new
posts, likes, hides, edits and deletions. A comment is ``item == "comment"``
AND ``verb == "add"``; anything looser answers the page's own new post as
though a customer had asked something.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import httpx

from app.channels.base import CommentChannelAdapter
from app.channels.config import ChannelSettings
from app.channels.constants import FACEBOOK_COMMENT
from app.channels.events import EVENT_TEXT, EVENT_UNSUPPORTED, InboundEvent
from app.channels.registry import register_adapter
from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger
from app.core.retry import http_retry

logger = get_logger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com"

#: The private reply travels the Messenger Send API, so it carries Messenger's
#: documented 2000-character limit.
DM_TEXT_MAX = 2000

#: Meta does not document a maximum length for a comment. This is therefore a
#: deliberately conservative bound rather than a verified platform figure:
#: truncating a very long answer is recoverable, and a 400 from the Graph API
#: means the customer gets no public answer at all. Raise it if Meta ever
#: publishes a real number.
COMMENT_TEXT_MAX = 2000

#: The Page webhook field that carries comments, and the two value fields that
#: narrow it to "a customer wrote a new comment".
FEED_FIELD = "feed"
COMMENT_ITEM = "comment"
ADD_VERB = "add"


@register_adapter
class FacebookCommentAdapter(CommentChannelAdapter):
    """Reads Page comment webhooks; replies publicly and, when asked, privately."""

    channel = FACEBOOK_COMMENT

    def __init__(self, settings: ChannelSettings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=GRAPH_API_BASE + "/" + settings.meta_api_version,
            headers={"Authorization": "Bearer " + settings.facebook_page_access_token},
            timeout=30.0,
        )

    # --- Outbound -----------------------------------------------------------

    @http_retry()
    async def _request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Single attempt; tenacity retries transient failures (429/5xx/network)."""
        response = await self._client.post(path, json=payload)
        response.raise_for_status()
        result = response.json()
        return result if isinstance(result, dict) else {}

    async def _post(
        self, path: str, payload: dict[str, Any], *, operation: str
    ) -> dict[str, Any]:
        """Send with retries, translating exhausted failures to a domain error.

        Raises ``ExternalServiceError`` like every other adapter, because
        ``_send_once`` upstream distinguishes a send that failed from one that
        never happened and can only do so if the error type is shared.
        """
        try:
            return await self._request(path, payload)
        except httpx.HTTPStatusError as exc:
            logger.error(
                "facebook_comment_api_error",
                operation=operation,
                status_code=exc.response.status_code,
                body=exc.response.text[:500],
            )
            raise ExternalServiceError("Facebook comment API request failed") from exc
        except httpx.HTTPError as exc:
            logger.error(
                "facebook_comment_network_error",
                operation=operation,
                error=str(exc),
            )
            raise ExternalServiceError("Facebook comment API unreachable") from exc

    async def reply_to_comment(self, comment_id: str, text: str) -> dict[str, Any]:
        """Post a public reply underneath the customer's comment."""
        return await self._post(
            "/" + comment_id + "/comments",
            {"message": text[:COMMENT_TEXT_MAX]},
            operation="public_reply",
        )

    async def invite_to_private_thread(
        self, comment_id: str, text: str
    ) -> dict[str, Any]:
        """Send the commenter a private reply, addressed to their comment.

        The recipient is the COMMENT, not a person: Meta resolves it to the
        commenter and returns their PSID in ``recipient_id``. That returned id
        is the only reliable link between a public comment and the private
        thread it produced, which is what makes comment-to-DM conversion
        measurable without joining users by external identity.

        No ``messaging_type`` is sent. It is part of the ordinary Send API
        contract, not the private reply one, and Meta's private reply
        documentation shows the body without it.

        Platform rules allow exactly one private reply per commenter, within
        seven days. A refusal is therefore ordinary traffic rather than an
        incident -- the public reply has already answered the customer -- so
        callers are expected to log the ``ExternalServiceError`` and move on.
        """
        page_id = self._settings.facebook_page_id.strip()
        if not page_id:
            # Would otherwise POST to "//messages", which is a confusing 404
            # rather than a statement about configuration.
            raise ExternalServiceError(
                "Facebook page id is not configured; cannot send a private reply"
            )
        return await self._post(
            "/" + page_id + "/messages",
            {
                "recipient": {"comment_id": comment_id},
                "message": {"text": text[:DM_TEXT_MAX]},
            },
            operation="private_reply",
        )

    async def send_text(self, recipient: str, text: str) -> dict[str, Any]:
        """Reply publicly. ``recipient`` is a COMMENT id, not a person id.

        ``base.py`` defines ``send_text`` as ChatService's sender contract, so
        an adapter is already a valid sender and none of the reservation,
        welcome or handoff bookkeeping needs a fork for comments. What changes
        on a public thread is only who is being answered: the reply belongs
        under the comment, so the comment id is what travels as the recipient.
        ``parse`` puts that same id in ``provider_message_id``, which is where
        callers get it from.
        """
        return await self.reply_to_comment(recipient, text)

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- Inbound ------------------------------------------------------------

    def parse(self, payload: dict[str, Any]) -> Iterable[InboundEvent]:
        """Turn one Page webhook delivery into normalised comment events.

        One delivery can carry several entries, each with several changes, and
        most changes are not comments at all.
        """
        events: list[InboundEvent] = []
        for entry in payload.get("entry") or []:
            if not isinstance(entry, dict):
                continue
            page_id = str(entry.get("id") or "")
            for change in entry.get("changes") or []:
                event = self._parse_change(change, page_id)
                if event is not None and event.routable:
                    events.append(event)
        return events

    def _parse_change(self, change: Any, page_id: str) -> InboundEvent | None:
        """One ``changes[]`` item, or None when it is not a customer comment."""
        if not isinstance(change, dict):
            return None
        if str(change.get("field") or "") != FEED_FIELD:
            return None

        value = change.get("value")
        if not isinstance(value, dict):
            return None
        if str(value.get("item") or "") != COMMENT_ITEM:
            return None
        if str(value.get("verb") or "") != ADD_VERB:
            return None

        comment_id = str(value.get("comment_id") or "")
        if not comment_id:
            # Without it there is nothing to reply to, nothing to dedupe on,
            # and nothing to key a private reply against.
            return None

        author = value.get("from")
        author = author if isinstance(author, dict) else {}
        sender_id = str(author.get("id") or "")

        if self._is_own_comment(sender_id, page_id):
            # The page's own replies come back as webhooks. Answering one
            # produces a reply that also arrives as a webhook, and the loop
            # costs a completion per turn until somebody notices.
            return None

        message = value.get("message")
        text = message if isinstance(message, str) and message.strip() else None

        return InboundEvent(
            channel=FACEBOOK_COMMENT,
            sender_id=sender_id,
            sender_name=author.get("name"),
            provider_message_id=comment_id,
            kind=EVENT_TEXT if text else EVENT_UNSUPPORTED,
            text=text,
            sent_at=self._timestamp(value.get("created_time")),
            context=self._context(value, page_id, comment_id),
        )

    def _is_own_comment(self, sender_id: str, page_id: str) -> bool:
        """Whether this comment was written by the page itself.

        Checked against the configured page id and against the id on the
        delivery. They are normally the same; when a token is moved between
        pages during setup they are briefly not, and the delivery is the one
        telling the truth about which page produced this comment.
        """
        if not self._settings.ignore_own_comments or not sender_id:
            return False
        configured = self._settings.facebook_page_id.strip()
        return sender_id == configured or (bool(page_id) and sender_id == page_id)

    @staticmethod
    def _context(
        value: dict[str, Any], page_id: str, comment_id: str
    ) -> dict[str, Any]:
        """The thread coordinates worth keeping with the event.

        ``post_id`` and ``parent_id`` are what make a reply land in the right
        thread; ``permalink_url`` is what an operator opens. They are carried
        on the event rather than looked up again later, because a second Graph
        API call to rediscover them can fail while the comment cannot.
        """
        context: dict[str, Any] = {"comment_id": comment_id, "page_id": page_id}
        for key in ("post_id", "parent_id", "permalink_url"):
            raw = value.get(key)
            if raw:
                context[key] = str(raw)
        return context

    @staticmethod
    def _timestamp(value: Any) -> datetime | None:
        """Read ``created_time``, which is epoch SECONDS on this field.

        Deliberately no division. Messenger and Instagram DMs send
        milliseconds; the Page ``feed`` field does not, and treating it as
        milliseconds silently backdates every comment to 1970.

        Returns None rather than raising on anything unreadable, matching the
        other adapters: the freshness gate fails open, and a format change on
        Meta's side must not silence every reply the bot makes.
        """
        try:
            return datetime.fromtimestamp(int(value), tz=UTC)
        except (TypeError, ValueError, OSError, OverflowError):
            logger.warning("facebook_comment_timestamp_unparseable", value=str(value))
            return None
