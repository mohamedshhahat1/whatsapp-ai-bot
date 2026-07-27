"""Conversation data access."""

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.models.conversation import Conversation
from app.repositories.base import BaseRepository


class ConversationRepository(BaseRepository):
    async def get(self, conversation_id: int) -> Conversation | None:
        return await self.session.get(Conversation, conversation_id)

    async def get_with_messages(self, conversation_id: int) -> Conversation | None:
        return await self.session.scalar(
            select(Conversation)
            .where(Conversation.id == conversation_id)
            .options(selectinload(Conversation.messages))
        )

    async def get_or_create_active(self, user_id: int) -> Conversation:
        conversation = await self.session.scalar(
            select(Conversation)
            .where(Conversation.user_id == user_id, Conversation.status == "active")
            .order_by(Conversation.created_at.desc())
            .limit(1)
        )
        if conversation is not None:
            return conversation
        conversation = Conversation(user_id=user_id, status="active")
        self.session.add(conversation)
        await self.session.flush()
        return conversation

    async def list(self, offset: int = 0, limit: int = 50) -> list[Conversation]:
        result = await self.session.scalars(
            select(Conversation)
            .order_by(Conversation.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result)

    async def delete(self, conversation: Conversation) -> None:
        await self.session.delete(conversation)

    async def count(self) -> int:
        return int(
            await self.session.scalar(select(func.count(Conversation.id))) or 0
        )
