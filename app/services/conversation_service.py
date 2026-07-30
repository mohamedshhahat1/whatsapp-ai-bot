"""Conversation management: persistence, history, context window."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.metrics import MESSAGES_TOTAL
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.repositories.conversation import ConversationRepository
from app.repositories.message import MessageRepository
from app.repositories.user import UserRepository
from app.utils.tokens import trim_history


class ConversationService:
    """Owns users, conversations, and message history handling."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._settings = settings
        self.users = UserRepository(session)
        self.conversations = ConversationRepository(session)
        self.messages = MessageRepository(session)

    async def get_context(
        self, wa_id: str, name: str | None = None
    ) -> tuple[User, Conversation]:
        """Resolve (and lazily create) the user and their active conversation."""
        user = await self.users.get_or_create(wa_id=wa_id, name=name)
        conversation = await self.conversations.get_or_create_active(user.id)
        return user, conversation

    async def claim_inbound(
        self,
        conversation_id: int,
        wa_message_id: str,
        type: str = "text",
        content: str | None = None,
        media_id: str | None = None,
    ) -> int | None:
        """Store an inbound message, or report that it was already stored.

        Returns the row id on success and ``None`` when this delivery has been
        seen before -- see ``MessageRepository.claim_inbound`` for why the
        check and the insert have to be the same statement.

        The metric is incremented only on a genuine claim. Counting
        redeliveries would inflate inbound volume by however aggressively Meta
        happened to retry, which is not a fact about the business.
        """
        message_id = await self.messages.claim_inbound(
            conversation_id=conversation_id,
            wa_message_id=wa_message_id,
            type=type,
            content=content,
            media_id=media_id,
        )
        if message_id is not None:
            MESSAGES_TOTAL.labels(direction="inbound", type=type).inc()
        return message_id

    async def save_inbound(
        self,
        conversation_id: int,
        wa_message_id: str | None,
        type: str = "text",
        content: str | None = None,
        media_id: str | None = None,
    ) -> Message:
        """Unconditionally store an inbound message.

        Retained for callers that have already established the message is new,
        and for tests. The webhook path uses :meth:`claim_inbound` instead,
        which is safe against concurrent redelivery.
        """
        MESSAGES_TOTAL.labels(direction="inbound", type=type).inc()
        return await self.messages.create(
            conversation_id=conversation_id,
            direction="inbound",
            type=type,
            content=content,
            wa_message_id=wa_message_id,
            media_id=media_id,
        )

    async def reserve_reply(
        self,
        conversation_id: int,
        reply_to_wa_message_id: str,
        content: str,
        type: str = "text",
    ) -> int | None:
        """Book the right to answer one inbound message.

        ``None`` means another attempt already holds it and this caller must
        not send. The outbound metric is incremented here rather than after the
        send: the reservation is the point at which we commit to a reply
        existing, and a send whose outcome is unknown still counts as one
        attempt to answer the customer.
        """
        message_id = await self.messages.reserve_reply(
            conversation_id=conversation_id,
            reply_to_wa_message_id=reply_to_wa_message_id,
            content=content,
            type=type,
        )
        if message_id is not None:
            MESSAGES_TOTAL.labels(direction="outbound", type=type).inc()
        return message_id

    async def save_outbound(
        self,
        conversation_id: int,
        content: str,
        wa_message_id: str | None = None,
        type: str = "text",
    ) -> Message:
        """Record an outbound message that has already been sent.

        Used by the dashboard's manual reply path, where an operator is
        watching the result and no retry will ever replay the send.
        """
        MESSAGES_TOTAL.labels(direction="outbound", type=type).inc()
        return await self.messages.create(
            conversation_id=conversation_id,
            direction="outbound",
            type=type,
            content=content,
            wa_message_id=wa_message_id,
            status="sent",
        )

    async def build_history(self, conversation_id: int) -> list[dict]:
        """Build a model-ready, token-budgeted message history."""
        messages = await self.messages.recent(
            conversation_id, limit=self._settings.max_context_messages * 2
        )
        history = [
            {
                "role": "user" if m.direction == "inbound" else "assistant",
                "content": m.content or f"[{m.type} message]",
            }
            for m in messages
        ]
        return trim_history(
            history,
            max_messages=self._settings.max_context_messages,
            max_tokens=self._settings.max_context_tokens,
        )
