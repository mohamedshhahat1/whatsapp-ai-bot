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

from inspect import Parameter, signature
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import EMBEDDING_DIMENSIONS
from app.repositories.ai_log import AILogRepository
from app.repositories.document import ChunkInput, DocumentRepository
from app.services.admin_service import AdminService
from app.services.ingestion import KnowledgeIngestionService


def _embedding() -> list[float]:
    """A non-zero vector: cosine distance is undefined for an all-zero one."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[0] = 1.0
    return vector


def _source() -> str:
    return "phase1c-" + uuid4().hex[:12] + ".md"


async def _add_document(
    session: AsyncSession, tenant_id: int, *, chunks: int = 0
) -> str:
    """Index one document under ``tenant_id`` and return its source path."""
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
                    content=f"chunk {index}",
                    token_count=3,
                    embedding=_embedding(),
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
        await logs.total_tokens()  # type: ignore[call-arg]


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
