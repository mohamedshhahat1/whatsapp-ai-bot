"""Document retrieval interface (RAG-ready).

The chat pipeline is already wired for retrieval-augmented generation: plug in
a real retriever (vector store, full-text search, API) by implementing the
``DocumentRetriever`` protocol and injecting it into ``ChatService``. Until
then, ``NullRetriever`` keeps the pipeline running without a knowledge base.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass
class RetrievedDocument:
    """One knowledge snippet retrieved for the current user message."""

    content: str
    source: str = "knowledge-base"
    score: float | None = None


class DocumentRetriever(Protocol):
    """Anything that can fetch relevant documents for a query."""

    async def retrieve(
        self, query: str, limit: int = 5
    ) -> list[RetrievedDocument]: ...


class NullRetriever:
    """Default no-op retriever used until a real knowledge base is added."""

    async def retrieve(
        self, query: str, limit: int = 5
    ) -> list[RetrievedDocument]:
        return []
