"""Knowledge-base data access, including vector similarity search."""

from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.orm import joinedload

from app.models.document import Document, DocumentChunk
from app.repositories.base import BaseRepository
from app.repositories.tenant import resolve_tenant_id


@dataclass
class ChunkInput:
    """A chunk ready to be persisted, with its embedding."""

    chunk_index: int
    content: str
    token_count: int
    embedding: list[float]
    page: int | None = None


@dataclass
class SearchHit:
    """One retrieved chunk with its similarity score."""

    chunk: DocumentChunk
    document: Document
    score: float


class DocumentRepository(BaseRepository):
    """CRUD for documents plus nearest-neighbour chunk search."""

    async def get_by_source(
        self, source: str, *, tenant_id: int | None = None
    ) -> Document | None:
        """Find a document by its path within one tenant's knowledge base.

        Scoped, unlike most reads in this phase, because ``upsert`` and
        ``delete_by_source`` are built on it: an unscoped lookup here means a
        second tenant uploading ``pricing.pdf`` finds the first tenant's row
        and overwrites its title, hash and every chunk under it. That is a
        write, not a read, so it could not wait for Phase 1c.

        ``tenant_id=None`` still means the deployment's original tenant, as it
        has since 0016. That is a write-path default and not an "all tenants"
        one -- it resolves to exactly one owner -- so Phase 1c leaves it
        alone rather than churning every 1b caller.
        """
        owner = await resolve_tenant_id(self.session, tenant_id)
        return await self.session.scalar(
            select(Document).where(
                Document.source == source,
                Document.tenant_id == owner,
            )
        )

    async def list_documents(self, *, tenant_id: int) -> list[Document]:
        """Every document in one tenant's knowledge base.

        ``tenant_id`` is keyword-only and deliberately has no default. An
        enumeration is the shape of read a default breaks silently: a caller
        that forgot to pass one would still be handed a list, just with other
        tenants' documents in it, and nothing in the response would say so.
        Mandatory makes the omission a TypeError at the call site instead of
        a leak found later.
        """
        statement = (
            select(Document)
            .where(Document.tenant_id == tenant_id)
            .order_by(Document.source)
        )
        result = await self.session.scalars(statement)
        return list(result)

    async def count_chunks(self, *, tenant_id: int) -> int:
        """How many chunks this tenant has indexed.

        Scoped even though nothing calls it today. A count across every
        tenant's corpus is a cross-tenant read whatever it is eventually
        wired to, and leaving the unscoped version in place would let the
        next caller inherit the defect rather than have to introduce it.
        """
        statement = select(func.count(DocumentChunk.id)).where(
            DocumentChunk.tenant_id == tenant_id
        )
        return int(await self.session.scalar(statement) or 0)

    async def upsert(
        self,
        source: str,
        title: str,
        content_hash: str,
        *,
        tenant_id: int | None = None,
    ) -> Document:
        """Create the document row, or update it in place if it already exists."""
        owner = await resolve_tenant_id(self.session, tenant_id)
        document = await self.get_by_source(source, tenant_id=owner)
        if document is None:
            document = Document(
                tenant_id=owner,
                source=source,
                title=title,
                content_hash=content_hash,
                chunk_count=0,
            )
            self.session.add(document)
            await self.session.flush()
            return document
        document.title = title
        document.content_hash = content_hash
        return document

    async def replace_chunks(
        self, document: Document, chunks: list[ChunkInput]
    ) -> None:
        """Atomically swap a document's chunks for a freshly embedded set.

        Chunks take their tenant from the document rather than from an
        argument, so there is no way for a caller to file one under the wrong
        owner -- and the composite foreign key would refuse it in any case.
        """
        await self.session.execute(
            delete(DocumentChunk).where(
                DocumentChunk.document_id == document.id,
                DocumentChunk.tenant_id == document.tenant_id,
            )
        )
        self.session.add_all(
            [
                DocumentChunk(
                    tenant_id=document.tenant_id,
                    document_id=document.id,
                    chunk_index=chunk.chunk_index,
                    page=chunk.page,
                    content=chunk.content,
                    token_count=chunk.token_count,
                    embedding=chunk.embedding,
                )
                for chunk in chunks
            ]
        )
        document.chunk_count = len(chunks)
        await self.session.flush()

    async def delete_by_source(
        self, source: str, *, tenant_id: int | None = None
    ) -> bool:
        """Remove a document and its chunks (cascade). True if it existed."""
        document = await self.get_by_source(source, tenant_id=tenant_id)
        if document is None:
            return False
        await self.session.delete(document)
        return True

    async def search(
        self, embedding: list[float], limit: int = 5, min_score: float = 0.0
    ) -> list[SearchHit]:
        """Return the closest chunks by cosine similarity.

        pgvector exposes *distance* (0 = identical). We convert to a 0-1
        similarity score so callers can reason in intuitive terms.

        Deliberately still unfiltered. ``document_chunks.tenant_id`` exists as
        of 0016 so that filtering is possible at all, but adding a predicate to
        an HNSW scan changes which index strategy is correct and how many rows
        have to be examined to still return ``limit`` of them. That is a
        measurement, and it belongs to the RAG phase rather than to a schema
        change -- a filter added here without one would quietly return fewer
        results than asked for.

        The correctness half of that argument no longer holds, and D-3 says
        so: this predicate lands in the next commit of this phase, together
        with the retriever and chat service it cannot be changed apart from.
        The measurement, and any index strategy that follows it, stays in
        Phase 4.
        """
        if not embedding:
            return []

        distance = DocumentChunk.embedding.cosine_distance(embedding).label("distance")
        statement = (
            select(DocumentChunk, distance)
            .options(joinedload(DocumentChunk.document))
            .order_by(distance)
            .limit(limit)
        )
        rows = (await self.session.execute(statement)).unique().all()

        hits: list[SearchHit] = []
        for chunk, chunk_distance in rows:
            score = 1.0 - float(chunk_distance)
            if score >= min_score:
                hits.append(
                    SearchHit(chunk=chunk, document=chunk.document, score=score)
                )
        return hits
