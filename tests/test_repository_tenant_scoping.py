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

Step 2c adds the customer and conversation reads to what was already covered
for documents, chunks and AI logs. Those are the paths an operator reaches by
naming an identifier, so they are authorization boundaries rather than
listings, and the tests below aim another tenant's id at each of them in both
directions.

Cleanup is not optional. ``other_tenant`` deletes its row on teardown and
every reference added in 0016 is ON DELETE RESTRICT, so a test that leaves a
document or an AI log behind fails in teardown -- which is the schema working,
not the test being fragile.
"""

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from inspect import Parameter, signature
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.tenant_context import system_tenant_context
from app.integrations.embeddings import EmbeddingClient
from app.models.conversation import MODE_HUMAN, TAG_SALES_LEAD
from app.models.document import EMBEDDING_DIMENSIONS
from app.repositories.ai_log import AILogRepository
from app.repositories.conversation import ConversationRepository
from app.repositories.document import ChunkInput, DocumentRepository
from app.repositories.message import MessageRepository
from app.repositories.user import UserRepository
from app.services.admin_service import AdminService
from app.services.chat_service import ChatService
from app.services.ingestion import KnowledgeIngestionService
from app.services.reply_service import ReplyService
from app.services.retrieval import PgVectorRetriever, build_retriever
from tests.conftest import Customer, create_customer, new_wa_id, purge

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


async def _add_message(
    session: AsyncSession, conversation_id: int, content: str
) -> None:
    """Store one inbound message on a conversation.

    The provider id is unique per call because ``wa_message_id`` carries a
    global unique index -- deliberately global, see MessageRepository -- so a
    constant here would collide with the second message this file writes.
    """
    await MessageRepository(session).create(
        conversation_id=conversation_id,
        direction="inbound",
        content=content,
        wa_message_id="wamid.phase1c." + uuid4().hex[:12],
    )
    await session.commit()


@pytest.fixture
async def two_customers(
    db: AsyncSession, default_tenant: int, other_tenant: int
) -> AsyncIterator[tuple[Customer, Customer]]:
    """The same phone number, as a customer of each tenant.

    Deliberately the SAME wa_id in both. That is the situation 0016 made
    legal when it replaced the global ``ix_users_wa_id`` with
    ``uq_users_tenant_wa_id``, and it is the one an unscoped lookup gets
    wrong: two rows match, and whichever Postgres returned first decided whose
    customer was answered. Seeding it here means these tests fail against the
    queries they replace rather than merely passing against the new ones.

    Teardown purges by wa_id, which is not tenant-scoped, so one call removes
    both sides -- and it has to, because ``other_tenant`` deletes its row
    afterwards and every reference added in 0016 is ON DELETE RESTRICT.
    """
    wa_id = new_wa_id()
    mine = await create_customer(db, wa_id, tenant_id=default_tenant)
    theirs = await create_customer(db, wa_id, tenant_id=other_tenant)
    try:
        yield mine, theirs
    finally:
        await purge(db, wa_id)


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


async def test_get_by_wa_id_resolves_within_the_asking_tenant(
    db: AsyncSession,
    default_tenant: int,
    other_tenant: int,
    two_customers: tuple[Customer, Customer],
) -> None:
    """One phone number, two tenants, two different customers.

    Unscoped this returned whichever row the planner reached first, so the
    inbound path could answer a customer out of another company's history.
    """
    mine, theirs = two_customers
    users = UserRepository(db)

    # The premise of the whole test: these really are two distinct rows.
    assert mine.user_id != theirs.user_id

    found = await users.get_by_wa_id(mine.wa_id, tenant_id=default_tenant)
    assert found is not None
    assert found.id == mine.user_id

    found = await users.get_by_wa_id(theirs.wa_id, tenant_id=other_tenant)
    assert found is not None
    assert found.id == theirs.user_id


async def test_get_conversation_does_not_reach_across_tenants(
    db: AsyncSession,
    default_tenant: int,
    other_tenant: int,
    two_customers: tuple[Customer, Customer],
) -> None:
    """The boundary behind every /admin route that names a conversation id.

    Asserted in both directions so the result cannot be an artefact of which
    tenant happens to own the older row.
    """
    mine, theirs = two_customers
    conversations = ConversationRepository(db)

    stolen = await conversations.get(theirs.conversation_id, tenant_id=default_tenant)
    assert stolen is None

    ours = await conversations.get(mine.conversation_id, tenant_id=default_tenant)
    assert ours is not None
    assert ours.id == mine.conversation_id

    stolen = await conversations.get(mine.conversation_id, tenant_id=other_tenant)
    assert stolen is None


async def test_get_with_messages_does_not_hand_over_a_transcript(
    db: AsyncSession,
    default_tenant: int,
    other_tenant: int,
    two_customers: tuple[Customer, Customer],
) -> None:
    """The worse of the two by-id reads: this one returns message bodies."""
    _, theirs = two_customers
    await _add_message(db, theirs.conversation_id, "our margin is confidential")
    conversations = ConversationRepository(db)

    stolen = await conversations.get_with_messages(
        theirs.conversation_id, tenant_id=default_tenant
    )
    assert stolen is None

    ours = await conversations.get_with_messages(
        theirs.conversation_id, tenant_id=other_tenant
    )
    assert ours is not None
    assert [message.content for message in ours.messages] == [
        "our margin is confidential"
    ]


async def test_listings_and_counts_stop_at_the_tenant_boundary(
    db: AsyncSession,
    default_tenant: int,
    other_tenant: int,
    two_customers: tuple[Customer, Customer],
) -> None:
    """Enumerations and counters, from both sides of the boundary."""
    _, theirs = two_customers
    users = UserRepository(db)
    conversations = ConversationRepository(db)
    messages = MessageRepository(db)

    await _add_message(db, theirs.conversation_id, "theirs")

    # The second tenant holds exactly one customer, one conversation and one
    # message, so its side is exact rather than a membership check.
    assert await users.count(tenant_id=other_tenant) == 1
    assert await conversations.count(tenant_id=other_tenant) == 1
    assert await messages.count(tenant_id=other_tenant) == 1

    listed = await users.list(tenant_id=other_tenant, limit=200)
    assert [user.id for user in listed] == [theirs.user_id]

    rows = await conversations.list(tenant_id=other_tenant, limit=200)
    assert [row.id for row in rows] == [theirs.conversation_id]

    # The default tenant carries rows from the rest of the suite and may hold
    # more than one page of them, so what can be asserted here is ownership
    # rather than a total.
    ours = await conversations.list(tenant_id=default_tenant, limit=200)
    assert theirs.conversation_id not in [row.id for row in ours]
    assert all(row.tenant_id == default_tenant for row in ours)


async def test_recent_message_and_lead_counters_are_per_tenant(
    db: AsyncSession,
    default_tenant: int,
    other_tenant: int,
    two_customers: tuple[Customer, Customer],
) -> None:
    """The two aggregates that feed the dashboard's live numbers."""
    _, theirs = two_customers
    messages = MessageRepository(db)
    conversations = ConversationRepository(db)

    since = datetime.now(UTC) - timedelta(hours=1)
    messages_before = await messages.count_since(since, tenant_id=default_tenant)
    leads_before = await conversations.count_unclaimed_leads(tenant_id=default_tenant)

    await _add_message(db, theirs.conversation_id, "just arrived")

    assert await messages.count_since(since, tenant_id=other_tenant) == 1
    # The other tenant's brand-new message must not appear in this window.
    assert await messages.count_since(since, tenant_id=default_tenant) == (
        messages_before
    )

    # Turn the other tenant's conversation into a genuinely unclaimed lead:
    # tagged, handed to a human, and assigned to nobody.
    lead = await conversations.get(theirs.conversation_id, tenant_id=other_tenant)
    assert lead is not None
    await conversations.set_mode(lead, MODE_HUMAN, tag=TAG_SALES_LEAD)
    await db.commit()

    assert await conversations.count_unclaimed_leads(tenant_id=other_tenant) == 1
    # And it must not inflate the other tenant's badge.
    assert await conversations.count_unclaimed_leads(tenant_id=default_tenant) == (
        leads_before
    )


async def test_the_sweep_helper_is_global_on_purpose(
    db: AsyncSession,
    default_tenant: int,
    other_tenant: int,
    two_customers: tuple[Customer, Customer],
) -> None:
    """The one deliberate exception, asserted rather than assumed.

    The idle sweep runs on a beat tick with no request behind it and no
    tenant to act for, so ``get_for_sweep`` reads any id it is given. What
    makes that safe is that the id comes from the sweep's own committed claim
    -- and what keeps it safe is the second half of this test: the scoped
    ``get`` beside it still refuses the same id, so the exception cannot
    quietly become a route to unscoped rows through the boundary method.
    """
    _, theirs = two_customers
    conversations = ConversationRepository(db)

    swept = await conversations.get_for_sweep(theirs.conversation_id)
    assert swept is not None
    assert swept.id == theirs.conversation_id

    scoped = await conversations.get(theirs.conversation_id, tenant_id=default_tenant)
    assert scoped is None


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


async def test_customer_and_conversation_reads_refuse_without_a_tenant(
    db: AsyncSession,
) -> None:
    """The same property for everything scoped in step 2c.

    ``get_for_sweep`` is absent from this list deliberately. It is the one
    read here that is meant to run without a tenant, and giving it a separate
    name rather than an optional argument is exactly what lets this test be
    exhaustive about the rest: there is no permissive branch to forget.
    """
    users = UserRepository(db)
    conversations = ConversationRepository(db)
    messages = MessageRepository(db)

    with pytest.raises(TypeError):
        await users.get_by_wa_id("whoever")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await users.get_by_channel_id("messenger", "psid")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await users.list()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await users.count()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await conversations.get(1)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await conversations.get_with_messages(1)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await conversations.list()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await conversations.count()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await conversations.count_unclaimed_leads()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await messages.count()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        await messages.count_since(datetime.now(UTC))  # type: ignore[call-arg]


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
    """Every service that reaches tenant-owned data demands a context.

    There is no reachable state in which one of these exists without knowing
    who it acts for, which is what stops a future caller reintroducing an
    unscoped enumeration -- or, in ReplyService's case, delivering a message
    into another tenant's conversation -- without noticing.
    """
    with pytest.raises(TypeError):
        AdminService(db)  # type: ignore[call-arg]

    # Read from the signature rather than by constructing one with a
    # deliberately wrong embedding client or WhatsApp client: the property
    # under test is that the parameter has no default, and this reads exactly
    # that.
    services: list[Callable[..., object]] = [ReplyService, KnowledgeIngestionService]
    for service in services:
        parameter = signature(service).parameters["tenant"]
        assert parameter.default is Parameter.empty, service
