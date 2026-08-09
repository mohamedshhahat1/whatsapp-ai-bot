"""Instagram comments: answer under the post, continue in the inbox.

An Instagram comment is not a Facebook Page comment with different ids, and
almost nothing in ``facebook_comments.py`` transfers unchanged. The webhook
arrives in two different envelopes depending on how the app authenticated, the
comment's id lives under a different key in each, there is no ``item``/``verb``
pair to narrow on, there is no timestamp at all, and the public reply is a
query-string edge rather than a JSON body.

Verified provider contract
--------------------------
Every shape below was read from Meta's documentation, not inferred from the
Facebook comment adapter or from the Messenger family:

- Instagram webhook field reference (``comments``, both payload shapes,
  ``media``, duplicate notifications on boosted posts):
  https://developers.facebook.com/docs/graph-api/webhooks/reference/instagram/
- Public replies -- ``POST /{ig-comment-id}/replies?message={message}``, with
  ``message`` documented as a query string parameter, top-level comments only:
  https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-comment/replies
- Private replies -- ``POST /<PAGE_ID>/messages`` with ``recipient:
  {comment_id}``, one per commenter, within 7 days:
  https://developers.facebook.com/documentation/business-messaging/instagram-messaging/features/private-replies

Four differences that would each have been silent
-------------------------------------------------
**Two id keys.** With Facebook Login for Business the comment arrives at
``entry[].changes[].value.comment_id``. With Instagram Login it arrives at
``entry[].value.id``, with no ``changes`` wrapper at all. Reading one key only
drops every comment from a whole authentication mode, and nothing raises:
the webhook still answers 200 and the comments simply never appear. Both are
accepted here, and both are pinned by a test.

**No timestamp.** The documented ``comments`` payload carries no
``created_time``. None is invented: ``sent_at`` stays None, which
``InboundEvent.age`` reads as fresh and the inbound freshness gate therefore
lets through. Manufacturing one from ``entry[].time`` would be a guess about
a unit that is not documented for this field -- and the Page adapter's
seconds-versus-milliseconds trap is exactly what guessing a unit costs.

**The private reply is addressed to the PAGE.** Not to the Instagram account,
and not to ``/me`` as Instagram DM sends are. Meta documents
``/<PAGE_ID>/messages`` for Instagram private replies and asks for the id of
the Facebook Page linked to the Instagram professional account. So this
surface needs ``FACEBOOK_PAGE_ID`` to send an invitation even though the
channel's own credentials are the Instagram pair. That is enforced where it is
used rather than as a channel-wide requirement, because a deployment that only
answers publicly never needs it.

**The public reply is a query parameter.** The reference documents ``message``
as a query string parameter on the ``/replies`` edge, so that is how it is
sent, rather than as the JSON body the Page ``/comments`` edge is given.
"""

from collections.abc import Iterable
from typing import Any

import httpx

from app.channels.base import CommentChannelAdapter
from app.channels.config import ChannelSettings
from app.channels.constants import INSTAGRAM_COMMENT
from app.channels.events import EVENT_TEXT, EVENT_UNSUPPORTED, InboundEvent
from app.channels.instagram import TEXT_MAX_BYTES, clip_utf8
from app.channels.registry import register_adapter
from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger
from app.core.retry import http_retry

logger = get_logger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com"

#: The Instagram webhook field carrying comments on posts, ads posts and reels.
#:
#: ``live_comments`` is a separate field for Instagram Live and is deliberately
#: not served here: a private reply to a live comment is only possible while
#: the broadcast is running, so treating one as an ordinary comment would
#: promise a follow-up that can no longer be delivered by the time it is tried.
COMMENTS_FIELD = "comments"

#: Meta documents no maximum length for a comment, so this is a deliberately
#: conservative bound rather than a verified platform figure: truncating a long
#: answer is recoverable, while a 400 from the Graph API means the customer
#: gets no public answer at all.
#:
#: Measured in BYTES rather than characters for the reason instagram.py sets
#: out at length: Instagram states its documented limits in bytes, and Arabic
#: is two bytes a letter in UTF-8.
COMMENT_TEXT_MAX_BYTES = 2000


@register_adapter
class InstagramCommentAdapter(CommentChannelAdapter):
    """Reads Instagram comment webhooks; replies publicly and, when asked, privately."""

    channel = INSTAGRAM_COMMENT

    def __init__(self, settings: ChannelSettings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=GRAPH_API_BASE + "/" + settings.meta_api_version,
            # instagram_token() falls back to the page token, which is what a
            # private reply needs anyway: Meta asks for a Page access token
            # carrying the MESSAGING task on the linked page.
            headers={"Authorization": "Bearer " + settings.instagram_token()},
            timeout=30.0,
        )

    # --- Outbound -----------------------------------------------------------

    @http_retry()
    async def _request(
        self,
        path: str,
        params: dict[str, str] | None,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Single attempt; tenacity retries transient failures (429/5xx/network)."""
        response = await self._client.post(path, params=params, json=payload)
        response.raise_for_status()
        result = response.json()
        return result if isinstance(result, dict) else {}

    async def _post(
        self,
        path: str,
        *,
        operation: str,
        params: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send with retries, translating exhausted failures to a domain error.

        Takes either a query string or a JSON body because this channel needs
        both: the ``/replies`` edge documents its ``message`` as a parameter,
        while a private reply's ``recipient``/``message`` objects can only be
        expressed as JSON.

        Raises ``ExternalServiceError`` like every other adapter, because
        ``_send_once`` upstream distinguishes a send that failed from one that
        never happened and can only do so if the error type is shared.
        """
        try:
            return await self._request(path, params, payload)
        except httpx.HTTPStatusError as exc:
            logger.error(
                "instagram_comment_api_error",
                operation=operation,
                status_code=exc.response.status_code,
                body=exc.response.text[:500],
            )
            raise ExternalServiceError("Instagram comment API request failed") from exc
        except httpx.HTTPError as exc:
            logger.error(
                "instagram_comment_network_error",
                operation=operation,
                error=str(exc),
            )
            raise ExternalServiceError("Instagram comment API unreachable") from exc

    async def reply_to_comment(self, comment_id: str, text: str) -> dict[str, Any]:
        """Post a public reply underneath the customer's comment.

        ``message`` travels as a query string parameter because that is how the
        ``/replies`` edge is documented.

        Only top-level comments can be replied to. Meta does not refuse a reply
        aimed at a reply -- it attaches it to the top-level comment instead --
        so there is nothing to guard against here, and inventing a guard would
        drop answers Meta would have accepted.
        """
        return await self._post(
            "/" + comment_id + "/replies",
            params={"message": clip_utf8(text, COMMENT_TEXT_MAX_BYTES)},
            operation="public_reply",
        )

    async def invite_to_private_thread(
        self, comment_id: str, text: str
    ) -> dict[str, Any]:
        """Send the commenter a private reply, addressed to their comment.

        The recipient is the COMMENT, not a person: Meta resolves it to the
        commenter and returns their Instagram-scoped id in ``recipient_id``.
        That returned id is the only reliable link between a public comment and
        the private thread it produced, which is what makes comment-to-DM
        conversion measurable without joining users by external identity --
        something this repository treats as two separate people on purpose.

        Addressed to the linked Facebook Page, which is what Meta documents for
        Instagram private replies. Not the Instagram account id, and not the
        ``/me/messages`` path Instagram DM sends use.

        No ``messaging_type`` is sent. It belongs to the ordinary Send API
        contract rather than the private reply one, and Meta's private reply
        documentation shows the body without it.

        Platform rules allow exactly one private reply per commenter, within
        seven days. A refusal is therefore ordinary traffic rather than an
        incident -- the public reply has already answered the customer -- so
        callers are expected to log the ``ExternalServiceError`` and move on.
        """
        page_id = self._settings.facebook_page_id.strip()
        if not page_id:
            # This channel's own credentials are the Instagram pair, so the
            # page id can legitimately be unset on a deployment that only
            # answers publicly. Said plainly rather than left to become a POST
            # to "//messages", which is a confusing 404.
            raise ExternalServiceError(
                "Facebook page id is not configured; "
                "cannot send an Instagram private reply"
            )
        return await self._post(
            "/" + page_id + "/messages",
            payload={
                "recipient": {"comment_id": comment_id},
                "message": {"text": clip_utf8(text, TEXT_MAX_BYTES)},
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
        """Turn one Instagram webhook delivery into normalised comment events.

        One delivery can carry several entries, each carrying several changes,
        and most changes are not comments at all.
        """
        events: list[InboundEvent] = []
        for entry in payload.get("entry") or []:
            if not isinstance(entry, dict):
                continue
            account_id = str(entry.get("id") or "")
            for change in self._changes(entry):
                event = self._parse_change(change, account_id)
                if event is not None and event.routable:
                    events.append(event)
        return events

    @staticmethod
    def _changes(entry: dict[str, Any]) -> list[Any]:
        """The change items on one entry, in either documented envelope.

        Facebook Login for Business nests them under ``changes``. Instagram
        Login puts a single ``field``/``value`` pair directly on the entry with
        no wrapper at all. Normalising here keeps ``_parse_change`` unaware of
        which login the app was built with, so neither shape can be the one
        that quietly stops working.
        """
        changes = entry.get("changes")
        if isinstance(changes, list):
            return changes
        if entry.get("field") is not None:
            return [entry]
        return []

    def _parse_change(self, change: Any, account_id: str) -> InboundEvent | None:
        """One change item, or None when it is not a customer's comment."""
        if not isinstance(change, dict):
            return None
        if str(change.get("field") or "") != COMMENTS_FIELD:
            return None

        value = change.get("value")
        if not isinstance(value, dict):
            return None

        comment_id = self._comment_id(value)
        if not comment_id:
            # Without it there is nothing to reply to, nothing to dedupe on,
            # and nothing to key a private reply against.
            return None

        author = value.get("from")
        author = author if isinstance(author, dict) else {}
        sender_id = str(author.get("id") or "")

        if self._is_own_comment(sender_id, account_id):
            # The account's own replies come back as webhooks. Answering one
            # produces a reply that also arrives as a webhook, and the loop
            # costs a completion per turn until somebody notices.
            return None

        raw_text = value.get("text")
        text = raw_text if isinstance(raw_text, str) and raw_text.strip() else None

        return InboundEvent(
            channel=INSTAGRAM_COMMENT,
            sender_id=sender_id,
            sender_name=author.get("username"),
            provider_message_id=comment_id,
            kind=EVENT_TEXT if text else EVENT_UNSUPPORTED,
            text=text,
            # Deliberately absent: see the module docstring. The documented
            # payload carries no timestamp, and the freshness gate fails open.
            sent_at=None,
            context=self._context(value, account_id, comment_id),
        )

    @staticmethod
    def _comment_id(value: dict[str, Any]) -> str:
        """The comment's id, under whichever key this login mode uses.

        ``comment_id`` with Facebook Login for Business, ``id`` with Instagram
        Login. Tried in that order because a payload carrying both is the
        Facebook Login shape, where ``id`` is not documented to be the comment.

        This value becomes ``provider_message_id`` and therefore the pipeline's
        idempotency key, which is what makes the duplicate notifications Meta
        documents for boosted and ads posts collapse into one stored comment.
        """
        for key in ("comment_id", "id"):
            candidate = value.get(key)
            if candidate:
                return str(candidate)
        return ""

    def _is_own_comment(self, sender_id: str, account_id: str) -> bool:
        """Whether this comment was written by the account itself.

        Checked against the configured Instagram account id and against the id
        on the delivery. They are normally the same; when a token is moved
        between accounts during setup they are briefly not, and the delivery is
        the one telling the truth about which account produced this comment.

        ``self_ig_scoped_id`` appears in the reference but its meaning for the
        ``comments`` field is not documented, so it is deliberately not read:
        guessing wrong in one direction answers our own comments, and in the
        other silently drops customers.
        """
        if not self._settings.ignore_own_comments or not sender_id:
            return False
        if sender_id == self._settings.instagram_account_id.strip():
            return True
        return bool(account_id) and sender_id == account_id

    @staticmethod
    def _context(
        value: dict[str, Any], account_id: str, comment_id: str
    ) -> dict[str, Any]:
        """The thread coordinates worth keeping with the event.

        ``parent_id`` tells a reply from a top-level comment. ``media``
        identifies the post, and carries ``ad_id``/``ad_title`` when the
        comment was left on a boosted or ads post -- which is the documented
        cause of duplicate notifications, so it is worth having on the event
        when the same comment turns up twice.

        Carried on the event rather than looked up again later, because a
        second Graph API call to rediscover them can fail while the comment
        cannot.
        """
        context: dict[str, Any] = {"comment_id": comment_id, "account_id": account_id}
        parent_id = value.get("parent_id")
        if parent_id:
            context["parent_id"] = str(parent_id)
        media = value.get("media")
        if isinstance(media, dict):
            if media.get("id"):
                context["media_id"] = str(media["id"])
            for key in ("media_product_type", "ad_id", "ad_title"):
                raw = media.get(key)
                if raw:
                    context[key] = str(raw)
        return context
