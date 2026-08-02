"""Operator-initiated (manual) replies from the admin dashboard."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.core.events import conversation_activity, publish
from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.integrations.whatsapp import WhatsAppClient
from app.models.message import Message
from app.models.user import User
from app.repositories.conversation import ConversationRepository
from app.repositories.message import MessageRepository

logger = get_logger(__name__)

# Meta only allows free-form messages within 24 hours of the customer's last
# message. Outside that window a pre-approved template is required.
CUSTOMER_SERVICE_WINDOW = timedelta(hours=24)


class OutsideServiceWindowError(ConflictError):
    """Raised when a free-form reply would be rejected by Meta.

    This is a 409, not a 500: the operator's request is valid, the
    conversation is simply in a state where Meta will not accept a free-form
    message. The dashboard shows the message verbatim, so the operator knows
    to use a template instead of retrying.
    """

    code = "outside_service_window"


class ReplyService:
    def __init__(
        self,
        session: AsyncSession,
        whatsapp: WhatsAppClient,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._whatsapp = whatsapp
        self._settings = settings or get_settings()
        self._conversations = ConversationRepository(session)
        self._messages = MessageRepository(session)

    async def send_manual_reply(self, conversation_id: int, text: str) -> Message:
        """Send an operator reply and record it in the conversation.

        The message is persisted only after the WhatsApp API accepts it, so a
        failed send never leaves a phantom message in the transcript.
        """
        conversation = await self._conversations.get(conversation_id)
        if conversation is None:
            raise NotFoundError(f"Conversation {conversation_id} not found")

        user = await self._session.get(User, conversation.user_id)
        if user is None:
            raise NotFoundError(f"User {conversation.user_id} not found")

        last_inbound = await self._messages.last_inbound_at(conversation_id)
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

        response = await self._whatsapp.send_text(user.wa_id, text)

        wa_message_id: str | None = None
        sent = response.get("messages")
        if isinstance(sent, list) and sent:
            wa_message_id = sent[0].get("id")

        message = await self._messages.create(
            conversation_id=conversation.id,
            direction="outbound",
            content=text,
            wa_message_id=wa_message_id,
            status="sent",
        )
        # An operator reply is activity like any other, so it resets the idle
        # timer. This matters most after the AI resumes: without it, a
        # conversation a person had been working for twenty minutes would be
        # eligible for closing the instant it went back to the bot, and the
        # customer would get a goodbye seconds after the operator's last word.
        await self._conversations.touch(conversation.id)
        await self._session.commit()
        logger.info(
            "manual_reply_sent",
            conversation_id=conversation.id,
            wa_message_id=wa_message_id,
        )
        # inbound=False: this refreshes any dashboard showing the conversation
        # (including a second operator's) without yanking anyone's attention
        # to it, since no customer is waiting on it.
        await publish(
            conversation_activity(conversation_id=conversation.id, inbound=False),
            self._settings,
        )
        return message
