"""Conversation data access."""

from datetime import UTC, datetime

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import selectinload

from app.core.exceptions import ConflictError
from app.models.conversation import MODE_HUMAN, TAG_SALES_LEAD, Conversation
from app.repositories.base import BaseRepository

# A sales lead nobody has claimed yet sorts above everything else. All three
# conditions matter:
#
#   tag                -- only leads jump the queue
#   mode = human       -- a conversation the AI has resumed is handled
#   no operator        -- once someone presses Take Over it is theirs, and
#                         leaving it pinned would keep drawing other operators
#                         to a row that is already being answered
#
# Without the last two the row would stay at the top of every operator's
# screen forever, which trains people to ignore the top of the list.
_UNCLAIMED_LEAD_FIRST = case(
    (
        (Conversation.tag == TAG_SALES_LEAD)
        & (Conversation.mode == MODE_HUMAN)
        & (Conversation.assigned_operator.is_(None)),
        0,
    ),
    else_=1,
)


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
        """The customer's open conversation, if any.

        Deliberately not filtered by ``mode``: a conversation owned by a human
        operator is still the customer's open conversation. Filtering it out
        here would make the next inbound message create a second one.
        """
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

        ``mode`` is not listed in the insert, so it comes from the column's
        server default rather than the ORM default.
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

    async def set_mode(
        self,
        conversation: Conversation,
        mode: str,
        operator: str | None = None,
        tag: str | None = None,
    ) -> Conversation:
        """Switch who answers this conversation.

        ``handoff_at`` records when the CURRENT handoff began, so it is cleared
        when the AI resumes; the same applies to ``assigned_operator``. Neither
        is an audit log -- the transitions are in the structured logs, and a
        real history would need its own table.

        ``tag`` behaves differently from both: it is only ever set, never
        cleared. A conversation that produced a sales lead produced one, even
        after the AI takes it back, and clearing it would make the lead vanish
        from tomorrow's report. Passing ``None`` leaves any existing tag alone
        rather than erasing it, so an ordinary later handoff cannot silently
        downgrade a conversation that was already classified.

        Does not commit: the caller owns the transaction boundary, because a
        handoff triggered by an inbound message must be committed together with
        that message.
        """
        conversation.mode = mode
        if tag is not None:
            conversation.tag = tag
        if mode == MODE_HUMAN:
            conversation.assigned_operator = operator
            conversation.handoff_at = datetime.now(UTC)
        else:
            conversation.assigned_operator = None
            conversation.handoff_at = None
        await self.session.flush()
        return conversation

    async def list(self, offset: int = 0, limit: int = 50) -> list[Conversation]:
        """Conversations for the operator list.

        Unclaimed sales leads first, then everything else by recency. Sorting
        here rather than in the dashboard means every client -- the web UI, a
        future mobile view, anyone reading the admin API -- gets the same
        order, and that pagination stays correct: ordering a page after it has
        been fetched only sorts the fifty rows that happened to be on it.
        """
        result = await self.session.scalars(
            select(Conversation)
            .order_by(_UNCLAIMED_LEAD_FIRST, Conversation.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result)

    async def delete(self, conversation: Conversation) -> None:
        await self.session.delete(conversation)

    async def count(self) -> int:
        return int(await self.session.scalar(select(func.count(Conversation.id))) or 0)

    async def count_unclaimed_leads(self) -> int:
        """Open sales leads with nobody on them -- the dashboard badge."""
        return int(
            await self.session.scalar(
                select(func.count(Conversation.id)).where(
                    Conversation.tag == TAG_SALES_LEAD,
                    Conversation.mode == MODE_HUMAN,
                    Conversation.assigned_operator.is_(None),
                )
            )
            or 0
        )
