"""Conversation data access."""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import selectinload

from app.core.exceptions import ConflictError
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

    async def active_for_user(self, user_id: int) -> Conversation | None:
        """The customer's open conversation, if any."""
        return await self.session.scalar(
            select(Conversation)
            .where(Conversation.user_id == user_id, Conversation.status == "active")
            .order_by(Conversation.created_at.desc())
            .limit(1)
        )

    async def get_or_create_active(self, user_id: int) -> Conversation:
        """Return the customer's active conversation, creating it atomically.

        Concurrent webhook deliveries used to be able to create two active
        conversations for one customer, which then split the history in half
        and halved the context the model saw. Migration
        0003_search_and_concurrency adds the partial unique index
        ``uq_active_conversation_per_user``; inferring that index in
        ``ON CONFLICT DO NOTHING`` makes the create side atomic and lets the
        loser re-read the winner's row.
        """
        conversation = await self.active_for_user(user_id)
        if conversation is not None:
            return conversation

        created_id = await self.session.scalar(
            pg_insert(Conversation)
            .values(user_id=user_id, status="active")
            .on_conflict_do_nothing(
                index_elements=[Conversation.user_id],
                index_where=Conversation.status == "active",
            )
            .returning(Conversation.id)
        )
        if created_id is not None:
            created = await self.session.get(Conversation, created_id)
            if created is not None:
                return created

        conversation = await self.active_for_user(user_id)
        if conversation is None:  # pragma: no cover - only if the winner rolled back
            raise ConflictError(
                f"Could not open an active conversation for user {user_id}"
            )
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
        return int(await self.session.scalar(select(func.count(Conversation.id))) or 0)
