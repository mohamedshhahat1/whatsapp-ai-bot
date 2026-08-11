"""Operator-initiated (manual) replies from the admin dashboard."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.outbound import (
    outbound_adapter,
    provider_message_id,
    recipient_id,
)
from app.config import Settings, get_settings
from app.core.events import conversation_activity, conversation_reopened, publish
from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.tenant_context import TenantContext
from app.integrations.whatsapp import WhatsAppClient
from app.models.conversation import STATUS_CLOSED, Conversation
from app.models.message import Message
from app.models.user import User
from app.repositories.conversation import ConversationRepository
from app.repositories.message import MessageRepository

logger = get_logger(__name__)

# Meta only allows free-form messages within 24 hours of the customer's last
# message. Outside that window a pre-approved template is required. The number
# is the same on every Meta surface -- see ChannelProfile.reply_window_hours --
# so one constant still covers every channel this service sends on.
CUSTOMER_SERVICE_WINDOW = timedelta(hours=24)


class OutsideServiceWindowError(ConflictError):
    """Raised when a free-form reply would be rejected by Meta.

    This is a 409, not a 500: the operator's request is valid, the
    conversation is simply in a state where Meta will not accept a free-form
    message. The dashboard shows the message verbatim, so the operator knows
    to use a template instead of retrying.
    """

    code = "outside_service_window"


class ConversationSupersededError(ConflictError):
    """Raised when a closed conversation cannot be revived to act on.

    Only one conversation per customer may be active at a time -- the partial
    unique index ``uq_active_conversation_per_user`` enforces it -- so a
    session that closed and was then followed by a NEW session can never be
    reopened. The customer has moved on to a different thread.

    Refusing is the only honest outcome. Sending anyway would deliver the
    message to the customer's phone but file it under a conversation they are
    no longer replying to, which is the orphaned-reply bug this class exists
    to prevent. The operator should open the customer's current conversation
    instead, and the 409 body tells them so.
    """

    code = "conversation_superseded"


class UnsupportedChannelError(ConflictError):
    """Raised when the customer's row carries no id to address them by.

    Outbound replies now route through the adapter registered for
    ``conversations.channel``, so the transport is no longer the limitation
    this class was originally written for. What remains is an identity
    problem: a row with neither ``external_id`` nor ``wa_id`` describes
    someone the platform cannot reach on any channel. That is a data fault
    rather than a missing feature, and the operator is still the person who
    needs to be told.

    A 409 rather than a 500 because the operator's request is well formed and
    retrying will not help. Kept as its own class, and with its original
    ``code``, because the dashboard and the Flutter app both branch on that
    string; renaming it would be a client-visible change for no gain.
    """

    code = "unsupported_channel"


async def revive_for_operator(
    conversations: ConversationRepository,
    conversation: Conversation,
    settings: Settings,
    session: AsyncSession,
    *,
    action: str,
) -> Conversation:
    """Ensure an operator is about to act on a live conversation.

    Shared by every operator entry point that writes to a conversation --
    manual reply, take over, resume AI -- because all of them had the same
    hole: none checked ``status``, so all of them could write into a session
    the sweeper had already closed.

    Returns the conversation untouched when it is already active, so the
    common path costs nothing. Otherwise it revives it through the single
    shared ``ConversationRepository.reopen`` (which clears ``closed_at`` and
    ``closing_sent_at`` while preserving ``welcome_sent_at`` and the whole
    transcript) and COMMITS before returning.

    Takes a loaded conversation rather than an id, which is what keeps it out
    of the tenant question entirely: every caller obtained this row from the
    tenant-scoped ``ConversationRepository.get``, so the ownership check has
    already happened and there is no identifier here to point at somebody
    else's row.

    Committing here rather than leaving it to the caller is deliberate: the
    callers go on to make a WhatsApp API call, and a revive that is still
    uncommitted when that call fails would roll back and leave the operator's
    delivered message attached to a closed row -- the exact bug being fixed.

    Raises :class:`ConversationSupersededError` when the customer has already
    started a newer session, since only one can be active at a time.
    """
    if conversation.status != STATUS_CLOSED:
        return conversation

    revived = await conversations.reopen(conversation.id)
    if revived is None:
        logger.info(
            "operator_action_on_superseded_conversation",
            conversation_id=conversation.id,
            action=action,
        )
        raise ConversationSupersededError(
            "This conversation has ended and the customer has since started a "
            "new one. Open their current conversation instead."
        )

    await session.commit()
    logger.info(
        "conversation_reopened_by_operator",
        conversation_id=revived.id,
        action=action,
    )
    await publish(
        conversation_reopened(
            conversation_id=revived.id,
            user_id=revived.user_id,
            status=revived.status,
            reason="operator",
            updated_at=revived.updated_at,
        ),
        settings,
    )
    return revived


class ReplyService:
    """Sends an operator's reply into one conversation, for one tenant.

    ``tenant`` is required. It is what makes the conversation lookup below an
    authorization boundary rather than a plain fetch by id: the id arrives
    from the request path, so without a tenant an operator could name any
    conversation in the deployment and have a message delivered into it.

    Positional and mandatory rather than defaulted. A default would have to
    invent a tenant, and the only tenant available to invent is the
    deployment's original one -- which is precisely the silent fallback D-6
    forbids. Every route reaches this through ``get_reply_service``, which
    resolves the context from the caller's own credential.
    """

    def __init__(
        self,
        session: AsyncSession,
        whatsapp: WhatsAppClient,
        tenant: TenantContext,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        # Still held, and still the process-wide singleton, but now handed to
        # the adapter factory rather than called directly. Keeping it on the
        # constructor means deps.get_reply_service and every existing test
        # that injects a fake client are unchanged.
        self._whatsapp = whatsapp
        self._tenant = tenant
        self._settings = settings or get_settings()
        self._conversations = ConversationRepository(session)
        self._messages = MessageRepository(session)

    def _resets_idle_timer(self) -> bool:
        """Whether an operator reply should reset the conversation's timer.

        The two flags are read here rather than delegated to SessionService
        because that module imports CUSTOMER_SERVICE_WINDOW from this one, and
        importing it back would be a cycle. Two booleans are not worth
        restructuring the dependency for.
        """
        if not self._settings.enable_conversation_session:
            return False
        return self._settings.reset_idle_timer_on_outgoing_message

    async def send_manual_reply(
        self, conversation_id: int, text: str, operator_id: int | None = None
    ) -> Message:
        """Send an operator reply and record it in the conversation.

        The message is persisted only after the platform accepts it, so a
        failed send never leaves a phantom message in the transcript.

        The lookup carries this service's tenant, so a conversation belonging
        to somebody else reads as absent and the caller raises the same 404 it
        raises for an id that does not exist. That is the answer that reveals
        least -- a 403 would confirm the conversation is real -- and it is the
        check that stops a message being delivered to another tenant's
        customer. Route-level authorization and its audit trail are step 4;
        this is the repository boundary underneath them.

        A closed conversation is revived before anything is sent. Replying
        into a closed session used to succeed and then strand the exchange:
        the message reached the customer, but their answer opened a different
        conversation, so the two halves of the same exchange lived in
        different threads.

        The reply leaves through the adapter for the conversation's own
        channel, so a Messenger customer is answered on Messenger. Refuses
        when the customer cannot be addressed at all
        (:class:`UnsupportedChannelError`) or when their channel is switched
        off or unconfigured (``ChannelUnavailableError``).

        ``operator_id`` defaults to None so that existing callers keep
        working; None simply means the reply is not attributed to an operator
        account, which is what a shared-key request produces.
        """
        conversation = await self._conversations.get(
            conversation_id, tenant_id=self._tenant.tenant_id
        )
        if conversation is None:
            raise NotFoundError(f"Conversation {conversation_id} not found")

        conversation = await revive_for_operator(
            self._conversations,
            conversation,
            self._settings,
            self._session,
            action="reply",
        )

        user = await self._session.get(User, conversation.user_id)
        if user is None:
            raise NotFoundError(f"User {conversation.user_id} not found")

        # Both reachability checks run before the service window, because "we
        # cannot reach you at all" and "we cannot reach you right now" are
        # different answers and the operator should be given the accurate one.
        # Nothing has been sent or written at this point, so refusing costs
        # nothing.
        recipient = recipient_id(user)
        if recipient is None:
            logger.info(
                "manual_reply_unaddressable_customer",
                conversation_id=conversation.id,
                channel=conversation.channel,
            )
            raise UnsupportedChannelError(
                "This customer has no contact id on record, so a reply cannot "
                "be delivered to them."
            )

        # Raises ChannelUnavailableError -- also a 409 -- when the channel is
        # switched off or enabled without credentials.
        adapter = outbound_adapter(conversation.channel, whatsapp_client=self._whatsapp)

        last_inbound = await self._messages.last_inbound_at(conversation.id)
        if last_inbound is None:
            raise OutsideServiceWindowError(
                "This customer has never messaged in; a free-form reply cannot "
                "be sent."
            )
        if datetime.now(UTC) - last_inbound > CUSTOMER_SERVICE_WINDOW:
            raise OutsideServiceWindowError(
                "The 24-hour WhatsApp service window has closed for this "
                "customer. Use an approved message template instead."
            )

        response = await adapter.send_text(recipient, text)

        # Reads both envelope shapes. The old inline lookup understood only
        # WhatsApp's, so a Messenger reply would have stored NULL here.
        wa_message_id = provider_message_id(response)

        message = await self._messages.create(
            conversation_id=conversation.id,
            direction="outbound",
            content=text,
            wa_message_id=wa_message_id,
            status="sent",
        )
        # Set on the returned row rather than threaded through
        # MessageRepository.create: that signature is shared with the bot
        # path, which never has an operator, so widening it would put an
        # always-None argument at every other call site. This lands in the
        # same transaction as the commit below.
        message.operator_id = operator_id
        # An operator reply is activity like any other, so it resets the idle
        # timer. This matters most after the AI resumes: without it, a
        # conversation a person had been working for twenty minutes would be
        # eligible for closing the instant it went back to the bot, and the
        # customer would get a goodbye seconds after the operator's last word.
        if self._resets_idle_timer():
            await self._conversations.touch(conversation.id)
        await self._session.commit()
        logger.info(
            "manual_reply_sent",
            conversation_id=conversation.id,
            channel=conversation.channel,
            wa_message_id=wa_message_id,
            operator_id=operator_id,
        )
        # inbound=False: this refreshes any dashboard showing the conversation
        # (including a second operator's) without yanking anyone's attention
        # to it, since no customer is waiting on it.
        await publish(
            conversation_activity(conversation_id=conversation.id, inbound=False),
            self._settings,
        )
        return message
