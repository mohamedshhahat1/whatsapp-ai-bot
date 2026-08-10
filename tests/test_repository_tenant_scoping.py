"""Two-tenant negative tests for the reads scoped in Phase 1c step 2.

Every test here seeds both tenants and then asserts an absence: that tenant
A's answer does not contain, count or sum anything belonging to tenant B.
Asserting only that A can see its own rows would pass just as happily against
the unscoped queries these replace, which is why the assertions are written
this way round.

The default tenant already holds rows from the rest of the suite, so its
figures are compared against a baseline taken inside the same test rather than
against a constant. The second tenant is created empty, so its figures are
exact.

Cleanup is not optional. ``other_tenant`` deletes its row on teardown and
every reference added in 0016 is ON DELETE RESTRICT, so a test that leaves a
document or an AI log behind fails in teardown -- which is the schema working,
not the test being fragile.
"""

from collections.abc import Callable
from inspect import Parameter, signature
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.tenant_context import system_tenant_context
from app.integrations.embeddings import EmbeddingClient
from app.models.document import EMBEDDING_DIMENSIONS
from app.repositories.ai_log import AILogRepository
from app.repositories.document import ChunkInput, DocumentRepository
from app.services.admin_service import AdminService
from app.services.chat_service import ChatService
from app.services.ingestion import KnowledgeIngestionService
from app.services.retrieval import PgVectorRetriever, build_retriever

#: Which embedding dimension the retrieval tests seed and query on. Chunks
#: written elsewhere in the suite sit on dimension 0, so a query on this axis
#: matches this file's rows exactly and everything else orthogonally. That
#: fixes the result order without requiring the rest of the table to be empty.
QUERY_AXIS = 1


def _embedding(axis: int = 0) -> list[float]:
    """A non-zero vector: cosine distance is undefined for an all-zero one.

    ``axis`` picks the dimension carrying the 1. Two vectors on different axes
    are orthogonal, which is what lets a test seed rows that are an exact
    match for its own query and a distant one for every other row in the
    table.
    """
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[axis] = 1.0
    return vector


def _source() -> str:
    return "phase1c-" + uuid4().hex[:12] + ".md"


class _FixedEmbeddings:
    """Embeds every query to the vector this file's fixtures store.

    Real embeddings would make the assertions depend on a model's opinion of
    two strings. What is under test is which rows a query is allowed to reach,
    not how well they rank, so a constant vector makes each seeded chunk an
    exact match and leaves the tenant predicate as the only thing that can
    remove one from the result.
    """

    async def embed_query(self, query: str) -> list[float]:
        return _embedding(QUERY_AXIS)


def _retriever(session: AsyncSession, tenant_id: int) -> PgVectorRetriever:
    """A retriever bound to one tenant, with the embedding call stubbed."""
    return PgVectorRetriever(
        session,
        cast(EmbeddingClient, _FixedEmbeddings()),
        get_settings(),
        system_tenant_context(tenant_id),
    )


async def _add_document(
    session: AsyncSession, tenant_id: int, *, chunks: int = 0, axis: int = 0
) -> str:
    """Index one document under ``tenant_id`` and return its source path.

    Chunk bodies carry the source, so a retrieval assertion can say which
    document a hit came from without joining anything back.
    """
    repository = DocumentRepository(session)
    source = _source()
    document = await repository.upsert(
        source=source,
        title="Phase 1c",
        content_hash=uuid4().hex,
        tenant_id=tenant_id,
    )
    if chunks:
        await repository.replace_chunks(
            document,
            [
                ChunkInput(
                    chunk_index=index,
                    content=f"{source} chunk {index}",
                    token_count=3,
                    embedding=_embedding(axis),
                )
                for index in range(chunks)
            ],
        )
    await session.commit()
    return source


async def _drop_documents(
    session: AsyncSession, tenant_id: int, sources: list[str]
) -> None:
    repository = DocumentRepository(session)
    for source in sources:
        await repository.delete_by_source(source, tenant_id=tenant_id)
    await session.commit()


async def _add_tokens(session: AsyncSession, tenant_id: int, total: int) -> None:
    await AILogRepository(session).create(
        model="gpt-phase1c", total_tokens=total, tenant_id=tenant_id
    )
    await session.commit()


async def _drop_ai_logs(session: AsyncSession, tenant_id: int) -> None:
    await session.execute(
        text("DELETE FROM ai_logs WHERE tenant_id = :tenant_id"),
        {"tenant_id": tenant_id},
    )
    await session.commit()


async def test_list_documents_does_not_return_another_tenants_documents(
    db: AsyncSession, default_tenant: int, other_tenant: int
) -> None:
    repository = DocumentRepository(db)
    mine = await _add_document(db, default_tenant)
    theirs = await _add_document(db, other_tenant)
    try:
        ours = [
            document.source
            for document in await repository.list_documents(tenant_id=default_tenant)
        ]
        assert mine in ours
        assert theirs not in ours

        # The second tenant was created empty, so this is exhaustive rather
        # than a membership check: it sees its own document and nothing else
        # the suite has ever indexed.
        others = await repository.list_documents(tenant_id=other_tenant)
        assert [document.source for document in others] == [theirs]
    finally:
        await _drop_documents(db, default_tenant, [mine])
        await _drop_documents(db, other_tenant, [theirs])


async def test_count_chunks_does_not_count_another_tenants_chunks(
    db: AsyncSession, default_tenant: int, other_tenant: int
) -> None:
    repository = DocumentRepository(db)
    before = await repository.count_chunks(tenant_id=default_tenant)
    theirs = await _add_document(db, other_tenant, chunks=3)
    try:
        assert await repository.count_chunks(tenant_id=other_tenant) == 3
        # The other tenant's three chunks must not show up here.
        assert await repository.count_chunks(tenant_id=default_tenant) == before
    finally:
        await _drop_documents(db, other_tenant, [theirs])


async def test_total_tokens_does_not_sum_another_tenants_spend(
    db: AsyncSession, default_tenant: int, other_tenant: int
) -> None:
    repository = AILogRepository(db)
    before = await repository.total_tokens(tenant_id=default_tenant)
    await _add_tokens(db, other_tenant, 4242)
    try:
        assert await repository.total_tokens(tenant_id=other_tenant) == 4242
        # Billing the wrong tenant is the failure this prevents.
        assert await repository.total_tokens(tenant_id=default_tenant) == before
    finally:
        await _drop_ai_logs(db, other_tenant)


async def test_search_does_not_return_another_tenants_chunks(
    db: AsyncSession, default_tenant: int, other_tenant: int
) -> None:
    """The read that decides what the model is told, proven scoped.

    This is the worst of the unscoped reads in this step. The others widen a
    listing or inflate a counter; this one quotes another company's private
    knowledge base into a customer's conversation, in the bot's own voice.
    """
    repository = DocumentRepository(db)
    mine = await _add_document(db, default_tenant, chunks=2, axis=QUERY_AXIS)
    theirs = await _add_document(db, other_tenant, chunks=2, axis=QUERY_AXIS)
    try:
        ours = await repository.search(
            _embedding(QUERY_AXIS), limit=50, tenant_id=default_tenant
        )
        # Ownership of every hit rather than a count: the default tenant
        # carries rows from the rest of the suite, so what can be asserted
        # exactly is who they belong to, not how many came back.
        assert all(hit.chunk.tenant_id == default_tenant for hit in ours)
        assert all(theirs not in hit.chunk.content for hit in ours)
        # Not a vacuous pass: the query really does reach this tenant's rows.
        assert any(mine in hit.chunk.content for hit in ours)

        # The second tenant was created empty, so this side is exhaustive.
        found = await repository.search(
            _embedding(QUERY_AXIS), limit=50, tenant_id=other_tenant
        )
        assert len(found) == 2
        assert all(theirs in hit.chunk.content for hit in found)
    finally:
        await _drop_documents(db, default_tenant, [mine])
        await _drop_documents(db, other_tenant, [theirs])


async def test_the_retriever_only_reaches_its_own_tenants_knowledge(
    db: AsyncSession, default_tenant: int, other_tenant: int
) -> None:
    """The same isolation one layer up, where ChatService consumes it.

    Worth asserting separately from the repository test: the tenant reaches
    the query through the retriever's constructor, so this is what proves the
    binding is actually carried into the search rather than merely stored.
    """
    theirs = await _add_document(db, other_tenant, chunks=2, axis=QUERY_AXIS)
    try:
        documents = await _retriever(db, default_tenant).retrieve("q", limit=50)
        assert all(theirs not in document.content for document in documents)

        found = await _retriever(db, other_tenant).retrieve("q", limit=50)
        assert found
        assert all(theirs in document.content for document in found)
    finally:
        await _drop_documents(db, other_tenant, [theirs])


async def test_scoped_reads_refuse_to_run_without_a_tenant(db: AsyncSession) -> None:
    """The scope cannot be omitted, only supplied.

    A default would have made each of these return a wider answer instead of
    failing, which is the whole class of bug this phase is closing.
    """
    documents = DocumentRepository(db)
    logs = AILogRepository(db)

    with pytest.raises(TypeError):
        await documents.list_documents()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await documents.count_chunks()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await documents.search(_embedding())  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await logs.total_tokens()  # type: ignore[call-arg]


async def test_retrieval_cannot_be_built_without_a_tenant() -> None:
    """No route to a retriever leaves the tenant out.

    Checked on the factory, the implementation and the service that owns one,
    because a default on any of the three would hand a caller a retriever that
    searches every tenant's corpus. Read from the signature rather than by
    constructing one: the property under test is that the parameter has no
    default, and this reads exactly that.
    """
    factories: list[Callable[..., object]] = [
        build_retriever,
        PgVectorRetriever,
        ChatService,
    ]
    for factory in factories:
        parameter = signature(factory).parameters["tenant"]
        assert parameter.default is Parameter.empty, factory


async def test_services_cannot_be_built_without_a_tenant(db: AsyncSession) -> None:
    """Both services that enumerate tenant-owned data demand a context.

    There is no reachable state in which one of these exists without knowing
    who it acts for, which is what stops a future caller reintroducing an
    unscoped enumeration without noticing.
    """
    with pytest.raises(TypeError):
        AdminService(db)  # type: ignore[call-arg]

    # Read from the signature rather than by constructing one with a
    # deliberately wrong embedding client: the property under test is that
    # the parameter has no default, and this reads exactly that.
    tenant = signature(KnowledgeIngestionService).parameters["tenant"]
    assert tenant.default is Parameter.empty
