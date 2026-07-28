"""Admin queries: listings and statistics."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.exceptions import NotFoundError
from app.models.conversation import Conversation
from app.models.document import Document
from app.models.user import User
from app.repositories.ai_log import AILogRepository
from app.repositories.conversation import ConversationRepository
from app.repositories.document import DocumentRepository
from app.repositories.message import MessageRepository
from app.repositories.user import UserRepository
from app.schemas.admin import StatsRead
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

    async def list_conversations(self, offset: int, limit: int) -> list[Conversation]:
        return await self._conversations.list(offset=offset, limit=limit)

    async def get_conversation(self, conversation_id: int) -> Conversation:
        conversation = await self._conversations.get_with_messages(conversation_id)
        if conversation is None:
            raise NotFoundError(f"Conversation {conversation_id} not found")
        return conversation

    async def delete_conversation(self, conversation_id: int) -> None:
        conversation = await self._conversations.get(conversation_id)
        if conversation is None:
            raise NotFoundError(f"Conversation {conversation_id} not found")
        await self._conversations.delete(conversation)
        await self._session.commit()

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
        since = datetime.now(UTC) - timedelta(hours=24)
        return StatsRead(
            total_users=await self._users.count(),
            total_conversations=await self._conversations.count(),
            total_messages=await self._messages.count(),
            messages_last_24h=await self._messages.count_since(since),
            total_tokens_used=await self._ai_logs.total_tokens(),
        )
