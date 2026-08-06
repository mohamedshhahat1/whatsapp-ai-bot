"""Admin queries: listings and statistics."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.events import conversation_handoff, publish
from app.core.exceptions import NotFoundError
from app.models.conversation import MODE_BOT, MODE_HUMAN, Conversation
from app.models.document import Document
from app.models.user import User
from app.repositories.ai_log import AILogRepository
from app.repositories.conversation import ConversationRepository
from app.repositories.document import DocumentRepository
from app.repositories.message import MessageRepository
from app.repositories.user import UserRepository
from app.schemas.admin import StatsRead
from app.services.reply_service import revive_for_operator
from app.services.retrieval import RetrievedDocument, build_retriever


class AdminService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._users = UserRepository(session)
        self._conversations = ConversationRepository(session)
        self._messages = MessageRepository(session)
        self._ai_logs = AILogRepository(session)
        self._documents = DocumentRepository(session)

    async def list_users(self, offset: int, limit: int) -> list[User]:
        return await self._users.list(offset=offset, limit=limit)

    async def list_conversations(
        self, offset: int, limit: int, status: str | None = None
    ) -> list[Conversation]:
        """Conversations for the operator list, newest-relevant first.

        ``status`` is optional and defaults to everything, so existing clients
        that never send it keep seeing exactly what they saw before. It exists
        because sessions now end: the table gained one row per visit instead
        of one per customer, and an operator looking for live work would
        otherwise scroll past a day of closed history to find it.
        """
        return await self._conversations.list(offset=offset, limit=limit, status=status)

    async def get_conversation(self, conversation_id: int) -> Conversation:
        conversation = await self._conversations.get_with_messages(conversation_id)
        if conversation is None:
            raise NotFoundError(f"Conversation {conversation_id} not found")
        return conversation

    async def conversation_history(
        self, conversation_id: int, limit: int = 20
    ) -> tuple[User, int, list[Conversation]]:
        """The customer behind a conversation, and their other visits.

        Returns the customer, how many conversations they have had in total,
        and the most recent others -- enough for the operator panel to say
        "5th visit" and link to the previous four.

        Sessions are deliberately NOT merged. They are separate visits and
        stitching them into one transcript would misrepresent what happened,
        hiding the gaps that are the whole point of the lifecycle. The operator
        gets navigation between them instead.

        This is an operator affordance only. None of it reaches the model:
        prompt context is still built from the current session alone, because
        silently widening what the AI remembers would change its answers in
        ways nobody asked for and would leak one visit's pricing talk into the
        next.
        """
        conversation = await self._conversations.get(conversation_id)
        if conversation is None:
            raise NotFoundError(f"Conversation {conversation_id} not found")

        user = await self._session.get(User, conversation.user_id)
        if user is None:  # pragma: no cover - FK guarantees this
            raise NotFoundError(f"User {conversation.user_id} not found")

        total = await self._conversations.count_for_user(conversation.user_id)
        others = await self._conversations.for_user(
            conversation.user_id, limit=limit, exclude_id=conversation_id
        )
        return user, total, others

    async def delete_conversation(self, conversation_id: int) -> None:
        conversation = await self._conversations.get(conversation_id)
        if conversation is None:
            raise NotFoundError(f"Conversation {conversation_id} not found")
        await self._conversations.delete(conversation)
        await self._session.commit()

    async def _switch_mode(
        self,
        conversation_id: int,
        mode: str,
        operator: str | None,
        reason: str,
        operator_id: int | None = None,
    ) -> Conversation:
        conversation = await self._conversations.get(conversation_id)
        if conversation is None:
            raise NotFoundError(f"Conversation {conversation_id} not found")

        # A closed session is revived before its mode changes, using the same
        # shared helper as the reply path. Without this the operator saw the
        # badge flip to "human" and believed they owned the customer, while
        # the customer's next message opened a different conversation in bot
        # mode -- so the takeover silently applied to a row nobody would ever
        # write to again. Raises ConversationSupersededError when the customer
        # has already moved on to a newer session.
        conversation = await revive_for_operator(
            self._conversations,
            conversation,
            get_settings(),
            self._session,
            action=reason,
        )

        await self._conversations.set_mode(conversation, mode, operator=operator)
        # The foreign-key half of the same fact. Set here rather than inside
        # set_mode because that repository method is shared with callers that
        # have only a label, and because the two columns must not be allowed
        # to disagree: whatever writes one writes the other.
        conversation.assigned_operator_id = operator_id
        await self._session.commit()
        # ``Conversation.updated_at`` is declared with ``onupdate=func.now()``,
        # so Postgres computes the new value and SQLAlchemy expires the
        # attribute after the UPDATE rather than guessing it. Left expired, the
        # next read is lazy IO -- and the next read is Pydantic serialising
        # ConversationRead, synchronously, which raises MissingGreenlet instead
        # of returning a timestamp. ``expire_on_commit=False`` does not help:
        # this expiry comes from the flush, not from the commit. Refresh here,
        # inside async code, where the IO is allowed.
        await self._session.refresh(conversation)
        # Published even when the mode did not actually change, so a second
        # operator's screen always reflects the current owner. The cost is one
        # redundant refetch; the alternative is two people typing to the same
        # customer because one of them saw a stale badge.
        await publish(
            conversation_handoff(
                conversation_id=conversation.id,
                mode=conversation.mode,
                assigned_operator=conversation.assigned_operator,
                reason=reason,
            ),
            get_settings(),
        )
        return conversation

    async def take_over(
        self,
        conversation_id: int,
        operator: str | None = None,
        operator_id: int | None = None,
    ) -> Conversation:
        """Give a conversation to a human operator; the bot stops answering.

        Idempotent: taking over a conversation that a human already owns just
        records the new operator.

        Reopens the session first if it has closed, so that the operator is
        never given a conversation the customer cannot reply into.

        ``operator`` is the display label and ``operator_id`` the account that
        now owns the conversation. Both are optional: a shared-key request has
        no account behind it, and the label was always optional.
        """
        return await self._switch_mode(
            conversation_id,
            MODE_HUMAN,
            operator=operator,
            reason="operator_took_over",
            operator_id=operator_id,
        )

    async def resume_ai(self, conversation_id: int) -> Conversation:
        """Hand the conversation back to the bot.

        The operator's messages stay in the transcript, so they are part of the
        history the model reads on the next turn: if a person corrected the
        bot, the bot sees the correction.

        Ownership is cleared, both the label and the account reference: once
        the bot is answering again, nobody owns the conversation. Who resumed
        it is a different question, and one the audit log answers.

        Note the interaction with the sweeper: resuming sets mode back to bot
        and resets the idle timer, which makes the session eligible for
        closing again from this moment. That is intended -- a conversation
        nobody is working should end like any other -- and the reset is what
        stops it ending immediately.
        """
        return await self._switch_mode(
            conversation_id,
            MODE_BOT,
            operator=None,
            reason="operator_resumed_ai",
            operator_id=None,
        )

    async def list_documents(self) -> list[Document]:
        """Knowledge-base documents currently indexed."""
        return await self._documents.list_documents()

    async def search_knowledge(
        self, query: str, limit: int = 5
    ) -> list[RetrievedDocument]:
        """Preview what RAG would feed the model for a given question."""
        retriever = build_retriever(self._session, get_settings())
        return await retriever.retrieve(query, limit=limit)

    async def stats(self) -> StatsRead:
        """Headline counters for the dashboard.

        ``total_conversations`` counts SESSIONS, not customers, and has done
        since sessions started closing themselves -- one returning customer
        now produces several. ``total_users`` is the customer count. The two
        used to be nearly interchangeable and no longer are; see
        ``StatsRead`` for the field-level wording.
        """
        since = datetime.now(UTC) - timedelta(hours=24)
        return StatsRead(
            total_users=await self._users.count(),
            total_conversations=await self._conversations.count(),
            total_messages=await self._messages.count(),
            messages_last_24h=await self._messages.count_since(since),
            total_tokens_used=await self._ai_logs.total_tokens(),
        )
