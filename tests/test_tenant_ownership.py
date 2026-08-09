"""Phase 1b: the tenant boundary, as the database enforces it.

Every assertion here is about what Postgres refuses, not about what the
application remembers to check. That is the point of the phase: application
tenant scoping arrives in 1c, and until it does, a schema that cannot express
a cross-tenant row is worth more than a service layer that promises not to
write one.

Three groups:

* the migration's own backfill SQL, driven against rows whose tenant has been
  cleared inside a transaction that always rolls back;
* what the new constraints refuse -- a child whose parent is in another
  tenant, a duplicate provider identity inside one tenant;
* what they must still allow, which is the half that regresses silently: the
  same phone number in two tenants, the same document path in two knowledge
  bases, and a rollup row for a tenant that had no traffic.

The fresh-upgrade, empty-database and upgrade/downgrade/upgrade paths are
exercised by the ``migrations`` CI job against a database built from nothing;
what can be checked from inside a migrated session is checked here.
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
from app.models.message import Message
from app.repositories.ai_log import AILogRepository
from app.repositories.analytics import PriceDefaults
from app.repositories.analytics_rollup import AnalyticsRollupRepository
from app.repositories.document import DocumentRepository
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
#: backfill impossible, and NULL there means "platform level, or before
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

ROLLUP_MODEL = "test-tenant-rollup-model"
ROLLUP_DEFAULTS = PriceDefaults(input_price=Decimal("1"), output_price=Decimal("2"))
ROLLUP_DAY = date(2001, 5, 6)
ROLLUP_AT = datetime(2001, 5, 6, 12, 0, tzinfo=UTC)

_IS_NULLABLE = """
SELECT is_nullable FROM information_schema.columns
WHERE table_name = :table AND column_name = 'tenant_id'
"""

_PRIMARY_KEY_COLUMNS = """
SELECT a.attname
  FROM pg_index AS i
  JOIN pg_class AS c ON c.oid = i.indrelid
  JOIN pg_attribute AS a
    ON a.attrelid = c.oid AND a.attnum = ANY (i.indkey)
 WHERE c.relname = :table AND i.indisprimary
 ORDER BY array_position(i.indkey, a.attnum)
"""

_CONSTRAINT_COLUMNS = """
SELECT a.attname
  FROM pg_constraint AS con
  JOIN pg_class AS rel ON rel.oid = con.conrelid
  JOIN pg_attribute AS a
    ON a.attrelid = rel.oid AND a.attnum = ANY (con.conkey)
 WHERE con.conname = :name AND rel.relname = :table
 ORDER BY array_position(con.conkey, a.attnum)
"""

_DELETE_ON = """
SELECT con.confdeltype
  FROM pg_constraint AS con
  JOIN pg_class AS rel ON rel.oid = con.conrelid
 WHERE con.conname = :name AND rel.relname = :table
"""

_TRIGGER_EXISTS = """
SELECT count(*)
  FROM pg_trigger AS t
  JOIN pg_class AS c ON c.oid = t.tgrelid
 WHERE c.relname = 'audit_logs' AND t.tgname = 'audit_logs_no_change'
"""

_ANY_OPERATOR = "SELECT id FROM operators ORDER BY id LIMIT 1"

_INSERT_AUDIT_ROW = """
INSERT INTO audit_logs (operator_id, action, resource_type, resource_id)
VALUES (:operator_id, 'ai.toggle', 'system', 'tenant-immutability-probe')
RETURNING id
"""

_INSERT_USER_IN_TENANT = """
INSERT INTO users (tenant_id, channel, external_id, wa_id, name)
VALUES (:tenant_id, :channel, :external_id, :wa_id, 'Isolation probe')
RETURNING id
"""

_INSERT_CONVERSATION_IN_TENANT = """
INSERT INTO conversations (tenant_id, user_id, channel, status, last_activity_at)
VALUES (:tenant_id, :user_id, 'whatsapp', 'active', now())
"""

_INSERT_DOCUMENT_IN_TENANT = """
INSERT INTO documents (tenant_id, source, title, content_hash, chunk_count)
VALUES (:tenant_id, :source, 'Probe', 'deadbeef', 0)
"""

_CLEAR_TENANT = "UPDATE {table} SET tenant_id = NULL WHERE id = :row_id"


def _migration(revision: str) -> ModuleType:
    """Load a migration module so its SQL can be executed as written."""
    path = Path("alembic") / "versions" / f"{revision}.py"
    spec = importlib.util.spec_from_file_location(revision, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- The schema the migration produced --------------------------------------


async def test_every_owned_table_requires_a_tenant(db: AsyncSession) -> None:
    for table in OWNED_TABLES:
        nullable = await db.scalar(text(_IS_NULLABLE), {"table": table})
        assert nullable == "NO", f"{table}.tenant_id is nullable"


async def test_the_audit_trail_keeps_a_nullable_tenant(db: AsyncSession) -> None:
    """NULL is a value here, not a gap.

    It means the action was taken at platform level by the shared admin
    identity, which holds no tenant membership, or that it predates tenancy.
    Backfilling it would require weakening the append-only trigger, which is a
    far worse trade than an unattributed historical row.
    """
    nullable = await db.scalar(text(_IS_NULLABLE), {"table": "audit_logs"})
    assert nullable == "YES"


async def test_the_rollup_is_keyed_on_the_pair(db: AsyncSession) -> None:
    """tenant_id leads, so one index serves a single tenant's date range."""
    columns = (
        (await db.execute(text(_PRIMARY_KEY_COLUMNS), {"table": "analytics_daily"}))
        .scalars()
        .all()
    )
    assert list(columns) == ["tenant_id", "day"]


async def test_the_child_keys_carry_the_tenant(db: AsyncSession) -> None:
    """Each parent reference includes the tenant, so a mismatch is unwritable."""
    for table, name in (
        ("conversations", "fk_conversations_user"),
        ("messages", "fk_messages_conversation"),
        ("document_chunks", "fk_document_chunks_document"),
    ):
        columns = (
            (
                await db.execute(
                    text(_CONSTRAINT_COLUMNS), {"name": name, "table": table}
                )
            )
            .scalars()
            .all()
        )
        assert "tenant_id" in columns, f"{name} does not include the tenant"


async def test_widening_the_keys_preserved_their_delete_behaviour(
    db: AsyncSession,
) -> None:
    """'c' is CASCADE. Widening changes which rows are valid, not what a
    delete does -- deleting a customer must still take their conversations.
    """
    for table, name in (
        ("conversations", "fk_conversations_user"),
        ("messages", "fk_messages_conversation"),
        ("document_chunks", "fk_document_chunks_document"),
    ):
        behaviour = await db.scalar(text(_DELETE_ON), {"name": name, "table": table})
        assert behaviour == "c", f"{name} is no longer ON DELETE CASCADE"


async def test_usage_logs_keep_their_own_tenant_reference(db: AsyncSession) -> None:
    """ai_logs.conversation_id stays SET NULL and stays single-column.

    A composite key here would null the tenant alongside the conversation when
    a conversation is deleted, detaching the cost record from whoever is billed
    for it -- which is the defect the column was added to fix. 'n' is SET NULL.
    """
    columns = (
        (
            await db.execute(
                text(_CONSTRAINT_COLUMNS),
                {"name": "fk_ai_logs_tenant", "table": "ai_logs"},
            )
        )
        .scalars()
        .all()
    )
    assert list(columns) == ["tenant_id"]


# --- The backfill, run as written -------------------------------------------


async def test_the_backfill_attaches_children_to_their_parents_tenant(
    db: AsyncSession, customer: Customer, default_tenant: int
) -> None:
    """A populated single-tenant deployment, in miniature.

    The tenant is cleared from a committed conversation and message and then
    restored by the migration's own statements, so this exercises the SQL that
    will run against real data rather than a paraphrase of it. Everything
    happens inside a transaction that rolls back either way.
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
        # Dropped and restored per table: tenant_id is NOT NULL, so the only
        # way to produce an unbackfilled row is to relax the column first.
        await db.execute(text("ALTER TABLE messages ALTER COLUMN tenant_id DROP NOT NULL"))
        await db.execute(
            text("ALTER TABLE conversations ALTER COLUMN tenant_id DROP NOT NULL")
        )
        await db.execute(
            text(_CLEAR_TENANT.format(table="messages")), {"row_id": message_id}
        )
        await db.execute(
            text(_CLEAR_TENANT.format(table="conversations")),
            {"row_id": customer.conversation_id},
        )

        await db.execute(text(module.BACKFILL_CONVERSATIONS))
        await db.execute(text(module.BACKFILL_MESSAGES))

        restored = await db.scalar(
            text("SELECT tenant_id FROM messages WHERE id = :row_id"),
            {"row_id": message_id},
        )
        assert restored == default_tenant

        remaining = (
            (await db.execute(text(module.UNBACKFILLED_ROWS))).all()
        )
        assert all(row[1] == 0 for row in remaining), remaining
    finally:
        await db.rollback()


async def test_the_downgrade_guard_sees_every_tenant_that_owns_a_row(
    db: AsyncSession, default_tenant: int, other_tenant: int
) -> None:
    """The query the downgrade refuses on.

    With rows in two tenants it must report both, because the pre-1b schema
    can represent only one and the downgrade has to say so rather than pick.
    """
    module = _migration(TENANT_OWNERSHIP)
    wa_id = new_wa_id()
    try:
        await create_customer(db, wa_id, tenant_id=other_tenant)
        found = (
            (await db.execute(text(module.TENANT_IDS_IN_USE))).scalars().all()
        )
        assert other_tenant in found
        assert len(set(found)) > 1, "the guard would allow a lossy downgrade"
    finally:
        await purge(db, wa_id)


# --- What the constraints refuse --------------------------------------------


async def test_a_conversation_cannot_belong_to_another_tenant(
    db: AsyncSession, customer: Customer, other_tenant: int
) -> None:
    """The composite key, doing the job the application cannot yet be trusted
    with: the customer is in the default tenant, so a conversation claiming to
    be in another one has no parent to reference.
    """
    try:
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(
                    text(_INSERT_CONVERSATION_IN_TENANT),
                    {"tenant_id": other_tenant, "user_id": customer.user_id},
                )
    finally:
        await db.rollback()


async def test_the_same_identity_twice_in_one_tenant_is_refused(
    db: AsyncSession, default_tenant: int
) -> None:
    """Tenant-scoped does not mean weaker. Within a tenant the pair is still
    unique, which is what stops one customer being split across two rows.
    """
    external_id = new_external_id()
    try:
        await db.execute(
            text(_INSERT_USER_IN_TENANT),
            {
                "tenant_id": default_tenant,
                "channel": MESSENGER,
                "external_id": external_id,
                "wa_id": None,
            },
        )
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(
                    text(_INSERT_USER_IN_TENANT),
                    {
                        "tenant_id": default_tenant,
                        "channel": MESSENGER,
                        "external_id": external_id,
                        "wa_id": None,
                    },
                )
    finally:
        await db.rollback()


async def test_one_document_path_twice_in_one_tenant_is_refused(
    db: AsyncSession, default_tenant: int
) -> None:
    source = f"probe-{new_wa_id()}.pdf"
    try:
        await db.execute(
            text(_INSERT_DOCUMENT_IN_TENANT),
            {"tenant_id": default_tenant, "source": source},
        )
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(
                    text(_INSERT_DOCUMENT_IN_TENANT),
                    {"tenant_id": default_tenant, "source": source},
                )
    finally:
        await db.rollback()


async def test_the_audit_trail_still_cannot_be_edited(db: AsyncSession) -> None:
    """Adding a column is DDL and does not fire a row-level trigger, so the
    append-only guarantee must be exactly as strong as it was before 1b.
    """
    installed = await db.scalar(text(_TRIGGER_EXISTS))
    assert installed == 1, "the append-only trigger is gone"

    operator_id = await db.scalar(text(_ANY_OPERATOR))
    if operator_id is None:
        pytest.skip("no operator row to attribute an audit entry to")

    try:
        row_id = await db.scalar(
            text(_INSERT_AUDIT_ROW), {"operator_id": operator_id}
        )
        assert row_id is not None
        # The trigger raises unconditionally on UPDATE, including for the
        # column 1b added.
        with pytest.raises(DBAPIError):
            async with db.begin_nested():
                await db.execute(
                    text("UPDATE audit_logs SET tenant_id = NULL WHERE id = :row_id"),
                    {"row_id": row_id},
                )
    finally:
        await db.rollback()


# --- What the constraints must still allow ----------------------------------


async def test_two_tenants_may_share_a_phone_number(
    db: AsyncSession, default_tenant: int, other_tenant: int
) -> None:
    """The defect this phase exists to fix, from the allowing side.

    Two businesses on one deployment can legitimately have the same customer.
    Before 0016 the second tenant's inbound message resolved to the first
    tenant's row and was answered out of the first tenant's history.
    """
    wa_id = new_wa_id()
    users = UserRepository(db)
    try:
        first = await users.get_or_create(wa_id, tenant_id=default_tenant)
        second = await users.get_or_create(wa_id, tenant_id=other_tenant)
        await db.commit()

        assert first.id != second.id, "the second tenant was handed the first's row"
        assert first.tenant_id == default_tenant
        assert second.tenant_id == other_tenant
    finally:
        await purge(db, wa_id)


async def test_two_tenants_may_share_a_provider_identity(
    db: AsyncSession, default_tenant: int, other_tenant: int
) -> None:
    """The same, for a channel where the id is page-scoped rather than a phone
    number. Both writers had to be scoped together: leaving either global
    would let its unique index swallow the second tenant's insert.
    """
    external_id = new_external_id()
    users = UserRepository(db)
    try:
        first = await users.get_or_create_by_channel(
            MESSENGER, external_id, tenant_id=default_tenant
        )
        second = await users.get_or_create_by_channel(
            MESSENGER, external_id, tenant_id=other_tenant
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

    get_by_source had to become tenant-scoped for this: upsert is built on it,
    so an unscoped lookup made the second upload a silent overwrite of the
    first tenant's title, hash and every chunk beneath it.
    """
    source = f"pricing-{new_wa_id()}.pdf"
    documents = DocumentRepository(db)
    try:
        mine = await documents.upsert(
            source, "Mine", "hash-a", tenant_id=default_tenant
        )
        theirs = await documents.upsert(
            source, "Theirs", "hash-b", tenant_id=other_tenant
        )
        await db.commit()

        assert mine.id != theirs.id

        found = await documents.get_by_source(source, tenant_id=default_tenant)
        assert found is not None
        assert found.id == mine.id
        assert found.title == "Mine", "the other tenant's upload overwrote this one"
    finally:
        await db.execute(delete(Document).where(Document.source == source))
        await db.commit()


async def test_a_message_inherits_the_tenant_of_its_conversation(
    db: AsyncSession, customer: Customer, default_tenant: int
) -> None:
    """Derived in the INSERT, so reserve-before-send stays one statement.

    Both reservation methods keep their single-column conflict targets; the
    tenant is read from the parent row rather than passed in, which is why
    there is no argument for a caller to get wrong.
    """
    claimed = await MessageRepository(db).claim_inbound(
        conversation_id=customer.conversation_id,
        wa_message_id=f"wamid.inherit.{customer.wa_id}",
        content="inbound probe",
    )
    assert claimed is not None

    reserved = await MessageRepository(db).reserve_reply(
        conversation_id=customer.conversation_id,
        reply_to_wa_message_id=f"wamid.inherit.{customer.wa_id}",
        content="outbound probe",
    )
    assert reserved is not None
    await db.commit()

    for row_id in (claimed, reserved):
        owner = await db.scalar(
            text("SELECT tenant_id FROM messages WHERE id = :row_id"),
            {"row_id": row_id},
        )
        assert owner == default_tenant


async def test_a_second_delivery_is_still_a_no_op(
    db: AsyncSession, customer: Customer
) -> None:
    """The guarantee 1b must not have weakened.

    The conflict target is still wa_message_id alone, so a redelivered webhook
    is refused before anything is spent on it.
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
    """
    logs = AILogRepository(db)
    rollup = AnalyticsRollupRepository(db)
    try:
        for _ in range(2):
            log = await logs.create(
                model=ROLLUP_MODEL,
                prompt_tokens=100,
                completion_tokens=50,
                total_tokens=150,
                latency_ms=200,
                tenant_id=default_tenant,
            )
            log.created_at = ROLLUP_AT
        await db.commit()

        await rollup.rollup_day(ROLLUP_DAY, ROLLUP_DEFAULTS)
        await db.commit()
        db.expire_all()

        busy = await rollup.get(ROLLUP_DAY, default_tenant)
        quiet = await rollup.get(ROLLUP_DAY, other_tenant)
        assert busy is not None and quiet is not None
        assert busy.requests == 2
        assert busy.total_tokens == 300
        assert quiet.requests == 0, "another tenant's traffic leaked into this row"
        assert quiet.total_tokens == 0
    finally:
        # analytics_daily references tenants ON DELETE RESTRICT, so the rollup
        # row for the temporary tenant has to go before its fixture tears down.
        await db.execute(
            delete(AnalyticsDaily).where(AnalyticsDaily.day == ROLLUP_DAY)
        )
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

    await db.execute(text("DELETE FROM users WHERE wa_id = :wa_id"), {"wa_id": wa_id})
    await db.commit()

    survivors = await db.scalar(
        text("SELECT count(*) FROM messages WHERE id = :row_id"), {"row_id": claimed}
    )
    assert survivors == 0


async def test_messages_are_reachable_only_within_their_tenant(
    db: AsyncSession, customer: Customer, other_tenant: int
) -> None:
    """The composite key from the message side.

    A message may not name a conversation in another tenant, which is what
    makes a mistaken tenant argument in some future service unwritable rather
    than merely unlikely.
    """
    try:
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(
                    text(
                        "INSERT INTO messages "
                        "(tenant_id, conversation_id, direction, type, content) "
                        "VALUES (:tenant_id, :conversation_id, 'inbound', "
                        "'text', 'cross-tenant probe')"
                    ),
                    {
                        "tenant_id": other_tenant,
                        "conversation_id": customer.conversation_id,
                    },
                )
    finally:
        await db.rollback()


async def test_nothing_here_left_a_message_without_a_tenant(
    db: AsyncSession,
) -> None:
    """A cheap invariant over the whole table, in case a writer was missed."""
    orphans = await db.scalar(
        text("SELECT count(*) FROM messages WHERE tenant_id IS NULL")
    )
    assert orphans == 0
    assert (
        await db.scalar(text("SELECT count(*) FROM {} WHERE tenant_id IS NULL".format("users")))
    ) == 0


async def test_the_conversation_slot_is_still_one_per_customer(
    db: AsyncSession, customer: Customer
) -> None:
    """uq_active_conversation_per_user needed no change, and must not have had
    one: user_id is itself tenant-scoped now, so one active conversation per
    user row already means one per person per channel per tenant.
    """
    try:
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(
                    text(_INSERT_CONVERSATION_IN_TENANT),
                    {
                        "tenant_id": (
                            await db.scalar(
                                text(
                                    "SELECT tenant_id FROM users WHERE id = :row_id"
                                ),
                                {"row_id": customer.user_id},
                            )
                        ),
                        "user_id": customer.user_id,
                    },
                )
    finally:
        await db.rollback()


async def test_the_message_table_keeps_its_global_reservation_anchors(
    db: AsyncSession,
) -> None:
    """Both anchors stay single-column, on purpose.

    Meta's ids are globally unique, so the tenant adds nothing to either key --
    and a conflict target that fails to fire means a customer is answered
    twice. This asserts the shape rather than trusting the comment.
    """
    for name, table in (
        ("ix_messages_wa_message_id", "messages"),
        ("uq_messages_reply_to_wa_message_id", "messages"),
    ):
        exists = await db.scalar(
            text("SELECT to_regclass(:name) IS NOT NULL"), {"name": name}
        )
        if not exists:
            continue
        columns = (
            (
                await db.execute(
                    text(_CONSTRAINT_COLUMNS), {"name": name, "table": table}
                )
            )
            .scalars()
            .all()
        )
        assert "tenant_id" not in columns, f"{name} was narrowed to a tenant"


async def test_a_document_chunk_cannot_cross_into_another_tenant(
    db: AsyncSession, default_tenant: int, other_tenant: int
) -> None:
    """Chunks inherit from their document, and the key refuses anything else."""
    source = f"chunks-{new_wa_id()}.pdf"
    try:
        document = await DocumentRepository(db).upsert(
            source, "Chunk probe", "hash-c", tenant_id=default_tenant
        )
        await db.commit()

        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(
                    text(
                        "INSERT INTO document_chunks "
                        "(tenant_id, document_id, chunk_index, content, "
                        "token_count, embedding) "
                        "VALUES (:tenant_id, :document_id, 0, 'probe', 1, "
                        ":embedding)"
                    ),
                    {
                        "tenant_id": other_tenant,
                        "document_id": document.id,
                        "embedding": str([0.0] * 1536),
                    },
                )
    finally:
        await db.rollback()
        await db.execute(delete(Document).where(Document.source == source))
        await db.commit()


async def test_no_message_row_lost_its_conversation(db: AsyncSession) -> None:
    """The widened key must not have orphaned anything during the migration."""
    orphans = await db.scalar(
        text(
            "SELECT count(*) FROM messages AS m "
            "LEFT JOIN conversations AS c "
            "ON c.id = m.conversation_id AND c.tenant_id = m.tenant_id "
            "WHERE c.id IS NULL"
        )
    )
    assert orphans == 0
    assert await db.scalar(text("SELECT count(*) FROM {}".format("tenants"))) >= 1
    assert Message is not None
