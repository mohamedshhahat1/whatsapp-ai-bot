"""Conversation management: persistence, history, context window."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
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

    async def save_inbound(
        self,
        conversation_id: int,
        wa_message_id: str | None,
        type: str = "text",
        content: str | None = None,
        media_id: str | None = None,
    ) -> Message:
        return await self.messages.create(
            conversation_id=conversation_id,
            direction="inbound",
            type=type,
            content=content,
            wa_message_id=wa_message_id,
            media_id=media_id,
        )

    async def save_outbound(
        self,
        conversation_id: int,
        content: str,
        wa_message_id: str | None = None,
        type: str = "text",
    ) -> Message:
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
