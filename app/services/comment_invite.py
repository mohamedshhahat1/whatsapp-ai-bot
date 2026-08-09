"""Inviting a commenter to continue in private.

A public comment is answered in public, and that answer is a normal trip
through ``ChatService`` like any other message. This module is the step that
can follow it: a private reply to the same comment, inviting the customer to
carry on somewhere they can share a phone number or a photo of their meter
without it being visible under the post.

Why this is not in ChatService
------------------------------
Nothing here is part of answering a message. ``ChatService`` is shared by
WhatsApp, Messenger and Instagram DM, and a comment-only branch inside it
would be stepped over on every one of those paths for the rest of its life.
Keeping the invitation outside means the three private channels run exactly
the code they ran before comments existed.

It is also not in the adapters. ``CommentChannelAdapter`` knows *how* to open
a private thread and deliberately not *whether* to -- that is a configuration
question with a different answer per page, and the copy is a setting all the
way down.

The idempotency key
-------------------
The reservation mechanism in ``MessageRepository`` already solves "exactly
one of these, even under concurrent redelivery", so this reuses it rather
than adding a table. The key is ``dm_invite:<comment_id>`` rather than the
bare comment id, and the prefix is load-bearing: the public reply reserves
the bare id, ``reply_to_wa_message_id`` is UNIQUE, and without the prefix the
two would compete for one row -- whichever ran second would be told the work
was already done and would silently send nothing.

With the prefix, the database enforces at most one invitation per comment.
Not application logic, not a cache key that can expire: a unique index, which
holds across workers, across processes, and across however many times Meta
redelivers the same comment. That guarantee matters more here than anywhere
else in the pipeline, because Meta permits exactly ONE private reply per
commenter -- see the contract note in the adapters.

What a failure does
-------------------
The reservation is committed before the provider call and kept, marked
unconfirmed, if that call fails. It is deliberately not released. Releasing
would invite a retry, and the failures that reach here are largely ambiguous
-- a timeout may well have delivered the invitation. A customer who never
receives one has still had their comment answered in public; a customer who
receives two unsolicited DMs from a business has been spammed by it.

Independence from the public reply
----------------------------------
Whether the invitation goes out does not depend on what the public answer
said, or on whether it succeeded. Meta's private reply window is keyed to the
comment and lasts seven days regardless. Tying the two together would mean a
Graph API failure on the public edge silently cancelled the private one, and
would make this function's behaviour depend on ChatService internals -- the
welcome rules, the quota guard, the handoff switch -- none of which have
anything to say about whether a page may message a commenter.

Provider contract: https://developers.facebook.com/documentation/business-messaging/messenger-platform/discovery/private-replies
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import CommentChannelAdapter
from app.channels.config import ChannelSettings, get_channel_settings
from app.channels.events import InboundEvent
from app.channels.outbound import provider_message_id
from app.config import Settings
from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger
from app.core.metrics import DUPLICATE_DELIVERIES_TOTAL
from app.models.message import STATUS_SENT, STATUS_UNCONFIRMED
from app.services.conversation_service import ConversationService

logger = get_logger(__name__)

#: Namespace for the invitation's reservation key. Keeps it out of the way of
#: the public reply, which reserves the bare comment id in the same unique
#: index. Changing this string would make every invitation already sent
#: invisible to the duplicate check, so it is a constant rather than an
#: f-string spelled out at the call site.
RESERVATION_PREFIX = "dm_invite:"

#: Stored on the outbound row so an invitation is distinguishable from an
#: ordinary reply without parsing the reservation key. The channel-aware
#: analytics work reads this to count comment-to-DM conversion; the key stays
#: an idempotency device rather than becoming a reporting dimension too.
INVITE_TYPE = "dm_invite"


def reservation_key(comment_id: str) -> str:
    """The idempotency key an invitation to ``comment_id`` is reserved under."""
    return RESERVATION_PREFIX + comment_id


async def invite_after_comment(
    session: AsyncSession,
    adapter: CommentChannelAdapter,
    settings: Settings,
    event: InboundEvent,
    channels: ChannelSettings | None = None,
) -> bool:
    """Send this commenter a private invitation, at most once, if configured.

    ``True`` only when an invitation actually left the process on this call.
    Every other outcome is ``False`` and is ordinary traffic rather than an
    error: the surface has invitations switched off, one has already been sent
    for this comment, or the provider refused it.

    Nothing here raises. The caller is serving a webhook that has already
    answered the customer in public, and an exception would fail the whole
    delivery -- sending it back through the queue to be retried, where the
    only thing that could differ is that the public answer is attempted a
    second time.
    """
    resolved = channels or get_channel_settings()
    channel = adapter.channel

    if not resolved.dm_invite_enabled(channel):
        return False

    text = resolved.dm_invite_message(channel)
    if not text.strip():
        # Unreachable through dm_invite_message, which falls back to the
        # packaged default when the override is blank. Kept because the cost
        # of being wrong is an empty direct message sent to a customer.
        logger.warning("dm_invite_copy_missing", channel=channel)
        return False

    comment_id = event.provider_message_id
    if not comment_id or not event.sender_id:
        # The same guard the router applies before dispatching: without both
        # ids there is nothing to address and nothing to dedupe on.
        return False

    conversations = ConversationService(session, settings)
    _, conversation = await conversations.get_channel_context(
        channel, event.sender_id, event.sender_name
    )

    # Read the primary key out once, while the instance is known to be
    # loaded. The duplicate branch below rolls back, and Session.rollback()
    # expires every object in the session -- expire_on_commit=False does not
    # cover it. Touching the ORM instance after that point emits a lazy
    # SELECT from a synchronous attribute access, which under asyncio raises
    # MissingGreenlet rather than returning a row. That branch is the
    # redelivery path, which is precisely where this must not raise: Meta
    # duplicates comment notifications by design.
    conversation_id = conversation.id

    reserved_id = await conversations.reserve_reply(
        conversation_id,
        reservation_key(comment_id),
        text,
        type=INVITE_TYPE,
    )
    if reserved_id is None:
        await session.rollback()
        DUPLICATE_DELIVERIES_TOTAL.labels(stage="dm_invite_reserved").inc()
        logger.info(
            "dm_invite_already_sent",
            channel=channel,
            conversation_id=conversation_id,
            comment_id=comment_id,
        )
        return False

    # Committed before the provider call, exactly as ChatService does for a
    # reply: a crash mid-send then leaves evidence, and the redelivery finds
    # the reservation and declines rather than sending a second invitation.
    await session.commit()

    try:
        result = await adapter.invite_to_private_thread(comment_id, text)
    except ExternalServiceError as exc:
        await conversations.messages.confirm_reply(
            reserved_id, None, status=STATUS_UNCONFIRMED
        )
        await session.commit()
        # Warning rather than error: platform rules refuse some of these by
        # design -- a commenter already privately replied to, a comment older
        # than the seven-day window, a page without the messaging permission.
        # The customer has their public answer either way.
        logger.warning(
            "dm_invite_failed",
            channel=channel,
            conversation_id=conversation_id,
            comment_id=comment_id,
            error=str(exc),
        )
        return False

    await conversations.messages.confirm_reply(
        reserved_id, provider_message_id(result), status=STATUS_SENT
    )
    await session.commit()

    # recipient_id is Meta resolving the comment to the person who wrote it,
    # and is the only trustworthy link between a public comment and the
    # private thread it produced. Logged here and not yet stored: persisting
    # it needs a column, and that migration belongs with the analytics work
    # that will read it.
    logger.info(
        "dm_invite_sent",
        channel=channel,
        conversation_id=conversation_id,
        comment_id=comment_id,
        recipient_id=str(result.get("recipient_id") or ""),
    )
    return True
