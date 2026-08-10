"""Document retrieval for retrieval-augmented generation.

``ChatService`` asks a ``DocumentRetriever`` for context before every AI call.
The pgvector-backed implementation embeds the customer's question with the
same model used at ingestion time and returns the closest knowledge chunks.

Retrieval is best-effort by design: if the knowledge base is empty, the query
is irrelevant, or the vector store is unreachable, the bot still answers from
its system prompt instead of failing the conversation.

Tenancy
-------
A retriever is bound to one tenant when it is constructed, not when it is
asked. Two things follow. Nothing that holds a retriever can widen it, so the
scope cannot drift between the request that resolved it and the query that
uses it. And ``retrieve`` keeps its one-argument shape, so the protocol below,
the null implementation and every existing call site are unchanged by tenant
isolation -- the filter reaches the database without appearing in the
interface that the rest of the application programs against.
"""

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.logging import get_logger
from app.core.tenant_context import TenantContext
from app.integrations.embeddings import EmbeddingClient, get_embedding_client
from app.repositories.document import DocumentRepository

logger = get_logger(__name__)


@dataclass
class RetrievedDocument:
    """One knowledge snippet retrieved for the current user message."""

    content: str
    source: str = "knowledge-base"
    score: float | None = None


class DocumentRetriever(Protocol):
    """Anything that can fetch relevant documents for a query."""

    async def retrieve(self, query: str, limit: int = 5) -> list[RetrievedDocument]: ...


class NullRetriever:
    """No-op retriever used when RAG is disabled."""

    async def retrieve(self, query: str, limit: int = 5) -> list[RetrievedDocument]:
        return []


class PgVectorRetriever:
    """Semantic search over one tenant's knowledge base."""

    def __init__(
        self,
        session: AsyncSession,
        embeddings: EmbeddingClient,
        settings: Settings,
        tenant: TenantContext,
    ) -> None:
        self._documents = DocumentRepository(session)
        self._embeddings = embeddings
        self._settings = settings
        self._tenant = tenant

    async def retrieve(self, query: str, limit: int = 5) -> list[RetrievedDocument]:
        """Embed the query and return the best-matching knowledge chunks."""
        cleaned = query.strip()
        if not cleaned:
            return []

        vector = await self._embeddings.embed_query(cleaned)
        hits = await self._documents.search(
            vector,
            limit=limit,
            min_score=self._settings.rag_min_score,
            tenant_id=self._tenant.tenant_id,
        )
        if not hits:
            logger.info("rag_no_match", query_length=len(cleaned))
            return []

        # Cap the total context so retrieval can never crowd out the
        # conversation history in the model's context window.
        budget = self._settings.rag_max_context_chars
        documents: list[RetrievedDocument] = []
        for hit in hits:
            content = hit.chunk.content
            if budget - len(content) < 0:
                break
            budget -= len(content)
            documents.append(
                RetrievedDocument(
                    content=content,
                    source=_label(hit.document.title, hit.chunk.page),
                    score=round(hit.score, 4),
                )
            )

        logger.info(
            "rag_retrieved",
            matches=len(documents),
            top_score=documents[0].score if documents else None,
        )
        return documents


def _label(title: str, page: int | None) -> str:
    """Human-readable citation shown to the model, e.g. 'Prices (p. 3)'."""
    return f"{title} (p. {page})" if page else title


def build_retriever(
    session: AsyncSession, settings: Settings, tenant: TenantContext
) -> DocumentRetriever:
    """Pick the retriever implementation for the current configuration.

    ``tenant`` is required even on the branch that returns a ``NullRetriever``
    and never reads a row. Making it conditional on ``rag_enabled`` would mean
    the argument that scopes the search is only demanded in the configuration
    where searching happens -- so a deployment that had RAG switched off would
    discover the missing scope by turning RAG on, in production, as a
    cross-tenant read.
    """
    if not settings.rag_enabled:
        return NullRetriever()
    return PgVectorRetriever(session, get_embedding_client(), settings, tenant)
