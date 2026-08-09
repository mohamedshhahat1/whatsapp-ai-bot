"""Phase 1b: the tenant boundary, as the database enforces it.

Every assertion here is about what Postgres refuses, not about what the
application remembers to check. That is the point of the phase: application
tenant scoping arrives in 1c, and until it does, a schema that cannot express a
cross-tenant row is worth more than a service layer that promises not to write
one.

The cross-tenant cases move an existing row rather than inserting a new one.
An INSERT would have to name every NOT NULL column in conversations, messages
and document_chunks, and a column missed there raises IntegrityError too -- so
the test would pass while proving nothing about the composite key. An UPDATE of
tenant_id alone can only fail on the constraint under test.

The fresh-upgrade, empty-database and upgrade/downgrade/upgrade paths run in
the ``migrations`` CI job against a database built from nothing. What can be
checked from inside an already-migrated session is checked here.
"""

import importlib.util
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.constants import MESSENGER
from app.models.ai_log import AILog
from app.models.analytics_rollup import AnalyticsDaily
from app.models.document import Document
from app.repositories.analytics import PriceDefaults
from app.repositories.analytics_rollup import AnalyticsRollupRepository
from app.repositories.document import ChunkInput, DocumentRepository
from app.repositories.message import MessageRepository
from app.repositories.user import UserRepository
from tests.conftest import (
    Customer,
    create_customer,
    new_external_id,
    new_wa_id,
    purge,
    purge_channel,
)

TENANT_OWNERSHIP = "0016_tenant_ownership"

#: Tables that must not accept a NULL tenant. audit_logs is deliberately
#: absent: its column is nullable because the append-only trigger makes a
#: backfill impossible, so NULL there means "platform level, or before
#: tenancy".
OWNED_TABLES = (
    "users",
    "conversations",
    "messages",
    "documents",
    "document_chunks",
    "ai_logs",
    "analytics_daily",
)

#: The three keys that gained the tenant. Each was ON DELETE CASCADE before 1b
#: and must still be: 'c' in pg_constraint.confdeltype.
WIDENED_KEYS = (
    ("conversations", "fk_conversations_user"),
    ("messages", "fk_messages_conversation"),
    ("document_chunks", "fk_document_chunks_document"),
)

ROLLUP_MODEL = "test-tenant-rollup-model"
ROLLUP_DEFAULTS = PriceDefaults(input_price=Decimal("1"), output_price=Decimal("2"))
ROLLUP_DAY = date(2001, 5, 6)
ROLLUP_AT = datetime(2001, 5, 6, 12, 0, tzinfo=UTC)

#: A chunk has to carry a vector of the column's exact width. The values are
#: irrelevant -- nothing here searches -- so the cheapest legal vector is used.
EMBEDDING = [0.0] * 1536

_IS_NULLABLE = """
SELECT is_nullable FROM information_schema.columns
WHERE table_name = :table AND column_name = 'tenant_id'
"""

_PRIMARY_KEY_COLUMNS = """
SELECT a.attname
  FROM pg_index AS i
  JOIN pg_class AS c ON c.oid = i.indrelid
  JOIN pg_attribute AS a ON a.attrelid = c.oid AND a.attnum = ANY (i.indkey)
 WHERE c.relname = :table AND i.indisprimary
 ORDER BY array_position(i.indkey, a.attnum)
"""

_CONSTRAINT_COLUMNS = """
SELECT a.attname
  FROM pg_constraint AS con
  JOIN pg_class AS rel ON rel.oid = con.conrelid
  JOIN pg_attribute AS a ON a.attrelid = rel.oid AND a.attnum = ANY (con.conkey)
 WHERE con.conname = :name AND rel.relname = :table
 ORDER BY array_position(con.conkey, a.attnum)
"""

# confdeltype is Postgres's internal one-byte "char", which asyncpg hands back
# as bytes rather than str -- b'c' == 'c' is False, so an uncast column would
# fail this test against a schema that is perfectly correct. Cast in SQL so the
# assertion compares what it appears to compare.
_DELETE_BEHAVIOUR = """
SELECT con.confdeltype::text
  FROM pg_constraint AS con
  JOIN pg_class AS rel ON rel.oid = con.conrelid
 WHERE con.conname = :name AND rel.relname = :table
"""

_AUDIT_TRIGGER = """
SELECT t.tgenabled = 'O'
   AND (t.tgtype & 2) <> 0
   AND (t.tgtype & 8) <> 0
   AND (t.tgtype & 16) <> 0
  FROM pg_trigger AS t
  JOIN pg_class AS c ON c.oid = t.tgrelid
 WHERE c.relname = 'audit_logs' AND t.tgname = 'audit_logs_no_change'
"""

_AUDIT_FUNCTION = """
SELECT pg_get_functiondef(oid)
  FROM pg_proc
 WHERE proname = 'audit_logs_immutable'
"""

_ANY_AUDIT_ROW = "SELECT id FROM audit_logs ORDER BY id LIMIT 1"

_TOUCH_AUDIT_ROW = """
UPDATE audit_logs SET tenant_id = NULL WHERE id = :row_id
"""

_RELAX_MESSAGES = "ALTER TABLE messages ALTER COLUMN tenant_id DROP NOT NULL"
_RELAX_CONVERSATIONS = """
ALTER TABLE conversations ALTER COLUMN tenant_id DROP NOT NULL
"""

_CLEAR_MESSAGE = "UPDATE messages SET tenant_id = NULL WHERE id = :row_id"
_CLEAR_CONVERSATION = """
UPDATE conversations SET tenant_id = NULL WHERE id = :row_id
"""

_TENANT_OF_MESSAGE = "SELECT tenant_id FROM messages WHERE id = :row_id"
_TENANT_OF_CONVERSATION = """
SELECT tenant_id FROM conversations WHERE id = :row_id
"""

_MOVE_CONVERSATION = """
UPDATE conversations SET tenant_id = :tenant_id WHERE id = :row_id
"""

_MOVE_MESSAGE = """
UPDATE messages SET tenant_id = :tenant_id WHERE id = :row_id
"""

_MOVE_CHUNKS = """
UPDATE document_chunks SET tenant_id = :tenant_id WHERE document_id = :row_id
"""

_RENAME_IDENTITY = """
UPDATE users SET external_id = :external_id WHERE id = :row_id
"""

_RENAME_SOURCE = "UPDATE documents SET source = :source WHERE id = :row_id"

_TENANT_OF_CHUNKS = """
SELECT DISTINCT tenant_id FROM document_chunks WHERE document_id = :row_id
"""

_SURVIVING_MESSAGE = "SELECT count(*) FROM messages WHERE id = :row_id"

_ORPHANED_MESSAGES = """
SELECT count(*)
  FROM messages AS m
  LEFT JOIN conversations AS c
    ON c.id = m.conversation_id AND c.tenant_id = m.tenant_id
 WHERE c.id IS NULL
"""

_RESERVATION_INDEX = """
SELECT to_regclass('ix_messages_wa_message_id') IS NOT NULL
"""


def _migration(revision: str) -> ModuleType:
    """Load a migration module so its own SQL can be executed as written."""
    path = Path("alembic") / "versions" / f"{revision}.py"
    spec = importlib.util.spec_from_file_location(revision, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _columns_of(db: AsyncSession, table: str, name: str) -> list[str]:
    rows = await db.execute(text(_CONSTRAINT_COLUMNS), {"name": name, "table": table})
    return list(rows.scalars())


# --- The schema the migration produced --------------------------------------


async def test_every_owned_table_requires_a_tenant(db: AsyncSession) -> None:
    for table in OWNED_TABLES:
        nullable = await db.scalar(text(_IS_NULLABLE), {"table": table})
        assert nullable == "NO", f"{table}.tenant_id is nullable"


async def test_the_audit_trail_keeps_a_nullable_tenant(db: AsyncSession) -> None:
    """NULL is a value here, not a gap.

    It means the action was taken at platform level by the shared admin
    identity, which holds no tenant membership, or that it predates tenancy.
    Backfilling it would mean weakening the append-only trigger, which is a far
    worse trade than an unattributed historical row.
    """
    nullable = await db.scalar(text(_IS_NULLABLE), {"table": "audit_logs"})
    assert nullable == "YES"


async def test_the_rollup_is_keyed_on_the_pair(db: AsyncSession) -> None:
    """tenant_id leads, so one index serves a single tenant's date range."""
    rows = await db.execute(text(_PRIMARY_KEY_COLUMNS), {"table": "analytics_daily"})
    assert list(rows.scalars()) == ["tenant_id", "day"]


async def test_the_child_keys_carry_the_tenant(db: AsyncSession) -> None:
    """Each parent reference includes the tenant, so a mismatch is unwritable."""
    for table, name in WIDENED_KEYS:
        columns = await _columns_of(db, table, name)
        assert "tenant_id" in columns, f"{name} does not include the tenant"


async def test_widening_the_keys_preserved_their_delete_behaviour(
    db: AsyncSession,
) -> None:
    """Widening changes which rows are valid, not what a delete does.

    Deleting a customer must still take their conversations and messages with
    it. 'c' is CASCADE.
    """
    for table, name in WIDENED_KEYS:
        behaviour = await db.scalar(
            text(_DELETE_BEHAVIOUR),
            {"name": name, "table": table},
        )
        assert behaviour == "c", f"{name} is no longer ON DELETE CASCADE"


async def test_usage_logs_keep_a_tenant_of_their_own(db: AsyncSession) -> None:
    """ai_logs.tenant_id is single-column, not composite with the conversation.

    conversation_id is nullable ON DELETE SET NULL. A composite key here would
    null the tenant alongside it, detaching the cost record from whoever is
    billed for it -- which is the defect the column was added to fix.
    """
    columns = await _columns_of(db, "ai_logs", "fk_ai_logs_tenant")
    assert columns == ["tenant_id"]


async def test_the_reservation_anchors_stayed_global(db: AsyncSession) -> None:
    """Both idempotency keys stay single-column, on purpose.

    Meta's ids are globally unique, so the tenant adds nothing to either key,
    and a conflict target that fails to fire means a customer is answered
    twice. Asserted rather than left to a comment.
    """
    columns = await _columns_of(
        db,
        "messages",
        "uq_messages_reply_to_wa_message_id",
    )
    # Equality rather than "tenant_id is absent": an empty list satisfies that
    # too, so a constraint dropped outright would read as one that had merely
    # stayed global. 0006 created it over this single column.
    assert columns == ["reply_to_wa_message_id"]

    assert await db.scalar(text(_RESERVATION_INDEX)), "inbound anchor is gone"


# --- The backfill, run as written -------------------------------------------


async def test_the_backfill_attaches_children_to_their_parents_tenant(
    db: AsyncSession, customer: Customer, default_tenant: int
) -> None:
    """A populated single-tenant deployment, in miniature.

    The tenant is cleared from a committed conversation and message and then
    restored by the migration's own statements, so this exercises the SQL that
    will run against real data rather than a paraphrase of it. NOT NULL has to
    be relaxed first, because otherwise there is no way to produce the
    unbackfilled row the statement exists to fix. All of it rolls back.
    """
    module = _migration(TENANT_OWNERSHIP)
    message_id = await MessageRepository(db).claim_inbound(
        conversation_id=customer.conversation_id,
        wa_message_id=f"wamid.backfill.{customer.wa_id}",
        content="backfill probe",
    )
    assert message_id is not None
    await db.commit()

    try:
        await db.execute(text(_RELAX_MESSAGES))
        await db.execute(text(_RELAX_CONVERSATIONS))
        await db.execute(text(_CLEAR_MESSAGE), {"row_id": message_id})
        await db.execute(
            text(_CLEAR_CONVERSATION),
            {"row_id": customer.conversation_id},
        )

        # Parent before child: a message takes its tenant from the
        # conversation, so the conversation has to have one first.
        await db.execute(text(module.BACKFILL_CONVERSATIONS))
        await db.execute(text(module.BACKFILL_MESSAGES))

        conversation_owner = await db.scalar(
            text(_TENANT_OF_CONVERSATION),
            {"row_id": customer.conversation_id},
        )
        message_owner = await db.scalar(
            text(_TENANT_OF_MESSAGE),
            {"row_id": message_id},
        )
        assert conversation_owner == default_tenant
        assert message_owner == default_tenant
    finally:
        await db.rollback()


async def test_the_downgrade_guard_sees_every_tenant_that_owns_a_row(
    db: AsyncSession, default_tenant: int, other_tenant: int
) -> None:
    """The query the downgrade refuses on.

    With rows in two tenants it must report both, because the pre-1b schema can
    represent only one: analytics_daily would need two rows under one primary
    key, and two tenants' customers sharing a phone number would collide on a
    global unique index. The downgrade has to say so rather than pick a winner.
    """
    module = _migration(TENANT_OWNERSHIP)
    wa_id = new_wa_id()
    try:
        await create_customer(db, wa_id, tenant_id=other_tenant)
        rows = await db.execute(text(module.TENANT_IDS_IN_USE))
        found = list(rows.scalars())
        assert other_tenant in found
        assert default_tenant in found
        assert len(set(found)) > 1, "the guard would allow a lossy downgrade"
    finally:
        await purge(db, wa_id)


# --- What the constraints refuse --------------------------------------------


async def test_a_conversation_cannot_move_to_another_tenant(
    db: AsyncSession, customer: Customer, other_tenant: int
) -> None:
    """The composite key doing the job the application cannot yet be trusted
    with: the customer is in the default tenant, so a conversation claiming to
    be in another one has no parent to reference.
    """
    try:
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(
                    text(_MOVE_CONVERSATION),
                    {
                        "tenant_id": other_tenant,
                        "row_id": customer.conversation_id,
                    },
                )
    finally:
        await db.rollback()


async def test_a_message_cannot_move_to_another_tenant(
    db: AsyncSession, customer: Customer, other_tenant: int
) -> None:
    """The same key from the child side, which is where a mistaken tenant
    argument in some future service would arrive.
    """
    message_id = await MessageRepository(db).claim_inbound(
        conversation_id=customer.conversation_id,
        wa_message_id=f"wamid.cross.{customer.wa_id}",
        content="cross-tenant probe",
    )
    assert message_id is not None
    await db.commit()

    try:
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(
                    text(_MOVE_MESSAGE),
                    {"tenant_id": other_tenant, "row_id": message_id},
                )
    finally:
        await db.rollback()


async def test_the_same_identity_twice_in_one_tenant_is_refused(
    db: AsyncSession, default_tenant: int
) -> None:
    """Tenant-scoped does not mean weaker.

    Within a tenant the pair is still unique, which is what stops one customer
    being split across two rows -- the race that the named ON CONFLICT clause
    in get_or_create_by_channel exists to lose safely.
    """
    users = UserRepository(db)
    mine = new_external_id()
    theirs = new_external_id()
    try:
        first = await users.get_or_create_by_channel(
            MESSENGER,
            mine,
            tenant_id=default_tenant,
        )
        await users.get_or_create_by_channel(
            MESSENGER,
            theirs,
            tenant_id=default_tenant,
        )
        await db.commit()

        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(
                    text(_RENAME_IDENTITY),
                    {"external_id": theirs, "row_id": first.id},
                )
    finally:
        await db.rollback()
        await purge_channel(db, MESSENGER, mine)
        await purge_channel(db, MESSENGER, theirs)


async def test_one_document_path_twice_in_one_tenant_is_refused(
    db: AsyncSession, default_tenant: int
) -> None:
    """Scoping the uniqueness to the tenant did not make it optional."""
    documents = DocumentRepository(db)
    mine = f"first-{new_wa_id()}.pdf"
    theirs = f"second-{new_wa_id()}.pdf"
    try:
        first = await documents.upsert(
            mine,
            "First",
            "hash-a",
            tenant_id=default_tenant,
        )
        await documents.upsert(
            theirs,
            "Second",
            "hash-b",
            tenant_id=default_tenant,
        )
        await db.commit()

        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(
                    text(_RENAME_SOURCE),
                    {"source": theirs, "row_id": first.id},
                )
    finally:
        await db.rollback()
        stale = delete(Document).where(Document.source.in_([mine, theirs]))
        await db.execute(stale)
        await db.commit()


async def test_a_chunk_cannot_move_away_from_its_document(
    db: AsyncSession, default_tenant: int, other_tenant: int
) -> None:
    """Chunks inherit from their document and the key refuses anything else.

    This is the constraint that makes tenant-filtered retrieval possible later:
    a chunk's tenant cannot disagree with its document's, so filtering on the
    denormalised column cannot return another tenant's text.
    """
    documents = DocumentRepository(db)
    source = f"chunks-{new_wa_id()}.pdf"
    try:
        document = await documents.upsert(
            source,
            "Chunk probe",
            "hash-c",
            tenant_id=default_tenant,
        )
        chunk = ChunkInput(
            chunk_index=0,
            content="probe",
            token_count=1,
            embedding=EMBEDDING,
        )
        await documents.replace_chunks(document, [chunk])
        await db.commit()

        # replace_chunks takes the tenant from the document rather than an
        # argument, so there is no way for a caller to file one wrongly.
        owners = await db.execute(text(_TENANT_OF_CHUNKS), {"row_id": document.id})
        assert list(owners.scalars()) == [default_tenant]

        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(
                    text(_MOVE_CHUNKS),
                    {"tenant_id": other_tenant, "row_id": document.id},
                )
    finally:
        await db.rollback()
        await db.execute(delete(Document).where(Document.source == source))
        await db.commit()


async def test_the_audit_trail_still_cannot_be_edited(db: AsyncSession) -> None:
    """Adding a column is DDL and does not fire a row-level trigger, so the
    append-only guarantee must be exactly as strong as it was before 1b.

    Checked from the catalogue first -- enabled, BEFORE, covering UPDATE and
    DELETE, function still raising -- because that holds whether or not this
    database happens to contain an audit row. When one does exist, the UPDATE
    is attempted for real.
    """
    intact = await db.scalar(text(_AUDIT_TRIGGER))
    assert intact, "the append-only trigger is missing, disabled or narrowed"

    body = await db.scalar(text(_AUDIT_FUNCTION))
    assert body is not None
    assert "audit_logs is append-only" in body

    row_id = await db.scalar(text(_ANY_AUDIT_ROW))
    if row_id is None:
        pytest.skip("no audit row exists to attempt an update against")

    try:
        with pytest.raises(DBAPIError):
            async with db.begin_nested():
                await db.execute(text(_TOUCH_AUDIT_ROW), {"row_id": row_id})
    finally:
        await db.rollback()


# --- What the constraints must still allow ----------------------------------


async def test_two_tenants_may_share_a_phone_number(
    db: AsyncSession, default_tenant: int, other_tenant: int
) -> None:
    """The defect this phase exists to fix, from the allowing side.

    Two businesses on one deployment can legitimately have the same customer.
    Before 0016 the second tenant's inbound message resolved to the first
    tenant's row, attached to that tenant's conversation, and was answered out
    of that tenant's history -- with no error anywhere, because the upsert's
    unnamed conflict target swallowed the collision.
    """
    wa_id = new_wa_id()
    users = UserRepository(db)
    try:
        first = await users.get_or_create(wa_id, tenant_id=default_tenant)
        second = await users.get_or_create(wa_id, tenant_id=other_tenant)
        await db.commit()

        assert first.id != second.id, "the second tenant got the first's row"
        assert first.tenant_id == default_tenant
        assert second.tenant_id == other_tenant
    finally:
        await purge(db, wa_id)


async def test_two_tenants_may_share_a_provider_identity(
    db: AsyncSession, default_tenant: int, other_tenant: int
) -> None:
    """The same, for a channel whose id is page-scoped rather than a phone
    number. Both writers had to be scoped in one step: leaving either global
    would let its unique index swallow the second tenant's insert.
    """
    external_id = new_external_id()
    users = UserRepository(db)
    try:
        first = await users.get_or_create_by_channel(
            MESSENGER,
            external_id,
            tenant_id=default_tenant,
        )
        second = await users.get_or_create_by_channel(
            MESSENGER,
            external_id,
            tenant_id=other_tenant,
        )
        await db.commit()

        assert first.id != second.id
        assert {first.tenant_id, second.tenant_id} == {default_tenant, other_tenant}
    finally:
        await purge_channel(db, MESSENGER, external_id)


async def test_two_tenants_may_upload_the_same_document(
    db: AsyncSession, default_tenant: int, other_tenant: int
) -> None:
    """Both may have a pricing.pdf, and neither may overwrite the other's.

    get_by_source had to become tenant-scoped for this, even though read paths
    are otherwise Phase 1c work: upsert is built on it, so an unscoped lookup
    made the second tenant's upload a silent overwrite of the first tenant's
    title, hash and every chunk beneath it. That is a write, not a read.
    """
    source = f"pricing-{new_wa_id()}.pdf"
    documents = DocumentRepository(db)
    try:
        mine = await documents.upsert(
            source,
            "Mine",
            "hash-a",
            tenant_id=default_tenant,
        )
        theirs = await documents.upsert(
            source,
            "Theirs",
            "hash-b",
            tenant_id=other_tenant,
        )
        await db.commit()

        assert mine.id != theirs.id

        found = await documents.get_by_source(source, tenant_id=default_tenant)
        assert found is not None
        assert found.id == mine.id
        assert found.title == "Mine", "the other tenant's upload overwrote it"
    finally:
        await db.execute(delete(Document).where(Document.source == source))
        await db.commit()


async def test_a_message_inherits_the_tenant_of_its_conversation(
    db: AsyncSession, customer: Customer, default_tenant: int
) -> None:
    """Derived inside the INSERT, so reserve-before-send stays one statement.

    Both reservation methods keep their single-column conflict targets; the
    tenant is read from the parent row in the same statement rather than passed
    in, which is why there is no argument for a caller to get wrong.
    """
    messages = MessageRepository(db)
    inbound = f"wamid.inherit.{customer.wa_id}"
    claimed = await messages.claim_inbound(
        conversation_id=customer.conversation_id,
        wa_message_id=inbound,
        content="inbound probe",
    )
    assert claimed is not None

    reserved = await messages.reserve_reply(
        conversation_id=customer.conversation_id,
        reply_to_wa_message_id=inbound,
        content="outbound probe",
    )
    assert reserved is not None
    await db.commit()

    for row_id in (claimed, reserved):
        owner = await db.scalar(text(_TENANT_OF_MESSAGE), {"row_id": row_id})
        assert owner == default_tenant


async def test_a_redelivery_is_still_a_no_op(
    db: AsyncSession, customer: Customer
) -> None:
    """The guarantee 1b must not have weakened.

    The conflict target is still wa_message_id alone, so a redelivered webhook
    is refused before anything is generated or spent on it.
    """
    wa_message_id = f"wamid.duplicate.{customer.wa_id}"
    messages = MessageRepository(db)

    first = await messages.claim_inbound(
        conversation_id=customer.conversation_id,
        wa_message_id=wa_message_id,
        content="first delivery",
    )
    await db.commit()
    second = await messages.claim_inbound(
        conversation_id=customer.conversation_id,
        wa_message_id=wa_message_id,
        content="redelivery",
    )
    await db.commit()

    assert first is not None
    assert second is None, "a redelivery was accepted as a new message"


async def test_the_rollup_writes_one_row_per_tenant(
    db: AsyncSession, default_tenant: int, other_tenant: int
) -> None:
    """Tenant-scoped analytics, including the tenant that did nothing.

    The statement is driven FROM tenants and LEFT JOINs the aggregates, so a
    tenant with no traffic gets a row of zeros rather than no row at all --
    which is what keeps "quiet day" distinguishable from "scheduler stopped".
    The logs are built directly, as in test_analytics_rollup, because the
    rollup counts API calls and these have no conversation.
    """
    rollup = AnalyticsRollupRepository(db)
    try:
        for _ in range(2):
            db.add(
                AILog(
                    tenant_id=default_tenant,
                    conversation_id=None,
                    model=ROLLUP_MODEL,
                    prompt_tokens=100,
                    completion_tokens=50,
                    total_tokens=150,
                    latency_ms=200,
                    created_at=ROLLUP_AT,
                )
            )
        await db.commit()

        await rollup.rollup_day(ROLLUP_DAY, ROLLUP_DEFAULTS)
        await db.commit()
        db.expire_all()

        busy = await rollup.get(ROLLUP_DAY, default_tenant)
        quiet = await rollup.get(ROLLUP_DAY, other_tenant)
        assert busy is not None
        assert quiet is not None
        assert busy.requests == 2
        assert busy.total_tokens == 300
        assert quiet.requests == 0, "another tenant's traffic leaked in"
        assert quiet.total_tokens == 0
    finally:
        # analytics_daily references tenants ON DELETE RESTRICT, so the row
        # written for the temporary tenant has to go before its fixture tears
        # that tenant down.
        rolled = delete(AnalyticsDaily).where(AnalyticsDaily.day == ROLLUP_DAY)
        await db.execute(rolled)
        await db.execute(delete(AILog).where(AILog.model == ROLLUP_MODEL))
        await db.commit()


async def test_deleting_a_customer_still_takes_their_messages(
    db: AsyncSession, other_tenant: int
) -> None:
    """CASCADE survived the widening, end to end rather than by catalogue."""
    wa_id = new_wa_id()
    created = await create_customer(db, wa_id, tenant_id=other_tenant)
    claimed = await MessageRepository(db).claim_inbound(
        conversation_id=created.conversation_id,
        wa_message_id=f"wamid.cascade.{wa_id}",
        content="cascade probe",
    )
    assert claimed is not None
    await db.commit()

    await purge(db, wa_id)

    survivors = await db.scalar(text(_SURVIVING_MESSAGE), {"row_id": claimed})
    assert survivors == 0


async def test_the_widened_key_orphaned_nothing(db: AsyncSession) -> None:
    """Every message still reaches its conversation through both columns.

    Cheap, and it covers the case a per-test assertion cannot: a writer
    somewhere else in the suite that files a message under the wrong tenant.
    """
    assert await db.scalar(text(_ORPHANED_MESSAGES)) == 0
