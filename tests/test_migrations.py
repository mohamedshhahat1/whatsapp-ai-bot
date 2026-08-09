"""C2 regression: a fresh database must be usable after `alembic upgrade head`.

The repository previously had no baseline migration, so the core tables only
existed if someone ran `alembic revision --autogenerate` by hand on the
server. The first group of tests inspects the migration graph; the second
checks what actually landed in the database CI migrated before pytest ran;
the third drives 0013's backfill against rows shaped like the ones it was
written for.
"""

import importlib.util
import inspect
from pathlib import Path
from types import ModuleType

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import new_wa_id

CORE_TABLES = ("users", "conversations", "messages", "ai_logs")

#: The expand/contract pair. 0009 added the columns and left external_id
#: nullable; 0013 backfills and tightens it.
CHANNEL_IDENTITY = "0009_channel_identity"
EXTERNAL_ID_NOT_NULL = "0013_external_id_not_null"

#: Phase 1b. Attaches the business tables to a tenant.
TENANT_OWNERSHIP = "0016_tenant_ownership"

_DROP_NOT_NULL = "ALTER TABLE users ALTER COLUMN external_id DROP NOT NULL"
_SET_NOT_NULL = "ALTER TABLE users ALTER COLUMN external_id SET NOT NULL"

_IS_NULLABLE = """
SELECT is_nullable FROM information_schema.columns
WHERE table_name = 'users' AND column_name = 'external_id'
"""

# INSERT ... SELECT rather than VALUES, because tenant_id is NOT NULL as of
# 0016 and its value is a sequence-assigned id nobody can hard-code. Resolving
# it from the slug keeps these fixtures independent of insertion order.
_INSERT_LEGACY_ROW = """
INSERT INTO users (tenant_id, channel, external_id, wa_id, name)
SELECT t.id, 'whatsapp', NULL, :wa_id, 'Pre-0013 row'
  FROM tenants AS t
 WHERE t.slug = 'default'
"""

_INSERT_MESSENGER_ROW = """
INSERT INTO users (tenant_id, channel, external_id, wa_id, name)
SELECT t.id, 'messenger', :external_id, NULL, 'Messenger row'
  FROM tenants AS t
 WHERE t.slug = 'default'
"""

_INSERT_UNREACHABLE_ROW = """
INSERT INTO users (tenant_id, channel, external_id, wa_id, name)
SELECT t.id, 'whatsapp', NULL, NULL, 'No id at all'
  FROM tenants AS t
 WHERE t.slug = 'default'
"""


def _scripts() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config("alembic.ini"))


def _migration(revision: str) -> ModuleType:
    """Load a migration module by file name, without an Alembic context.

    ``alembic/versions`` is not an importable package. Loading it is worth the
    trouble because these tests then run the migration's own SQL: a test that
    restated the UPDATE would keep passing after somebody changed the real one.
    """
    path = Path("alembic") / "versions" / f"{revision}.py"
    spec = importlib.util.spec_from_file_location(revision, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- The migration graph ----------------------------------------------------


def test_migration_history_has_a_single_head() -> None:
    """Two heads would make `alembic upgrade head` ambiguous and fail."""
    assert len(_scripts().get_heads()) == 1


def test_migration_history_is_a_single_chain_from_the_baseline() -> None:
    revisions = list(_scripts().walk_revisions())
    # Exactly one revision may be the base (down_revision is None).
    bases = [r.revision for r in revisions if r.down_revision is None]
    assert bases == ["0000_initial_schema"]
    assert len(revisions) >= 4


def test_the_expand_contract_pair_is_both_present_and_in_order() -> None:
    """0009 expands, 0013 contracts.

    Reordering them would set NOT NULL on a column that does not exist yet;
    dropping 0013 out of the chain would leave the writer filling a column
    nothing requires, which is the state this pair exists to end.
    """
    walked = [r.revision for r in _scripts().walk_revisions()]
    assert CHANNEL_IDENTITY in walked
    assert EXTERNAL_ID_NOT_NULL in walked
    assert walked.index(EXTERNAL_ID_NOT_NULL) < walked.index(CHANNEL_IDENTITY)


def test_the_contract_migration_follows_the_current_head_of_its_time() -> None:
    revision = _scripts().get_revision(EXTERNAL_ID_NOT_NULL)
    assert revision.down_revision == "0012_audit_retention"


def test_the_upgrade_backfills_before_it_tightens() -> None:
    """Order is the whole design. SET NOT NULL first aborts on every legacy row."""
    source = inspect.getsource(_migration(EXTERNAL_ID_NOT_NULL).upgrade)
    assert source.index("BACKFILL") < source.index("alter_column")


def test_the_downgrade_only_relaxes_the_constraint() -> None:
    """A source-level guard on a deliberate omission.

    Reversing the backfill is the tempting thing to write here and it would be
    destructive: the same column holds Messenger PSIDs and Instagram IGSIDs,
    which were never optional and cannot be reconstructed from wa_id. The
    downgrade drops the constraint and leaves every value alone, and any data
    change added later would have to arrive as an op.execute.
    """
    source = inspect.getsource(_migration(EXTERNAL_ID_NOT_NULL).downgrade)
    assert "nullable=True" in source
    assert "op.execute" not in source


def test_tenant_ownership_follows_the_tenancy_foundation() -> None:
    """1b builds on 1a. Reordering them would add a key to a missing table."""
    revision = _scripts().get_revision(TENANT_OWNERSHIP)
    assert revision.down_revision == "0015_tenancy_foundation"


def test_the_tenant_backfill_runs_before_the_columns_are_tightened() -> None:
    """Same expand/contract discipline as 0013, one table wider.

    SET NOT NULL before the backfill would abort on every existing row, which
    on a populated deployment means the migration fails after having taken an
    ACCESS EXCLUSIVE lock for nothing.
    """
    source = inspect.getsource(_migration(TENANT_OWNERSHIP).upgrade)
    assert source.index("add_column") < source.index("_backfill")
    assert source.index("_backfill") < source.index("alter_column")


def test_the_tenant_downgrade_refuses_rather_than_destroying_data() -> None:
    """The pre-1b schema cannot represent two tenants, so it must not try.

    analytics_daily would need two rows under one primary key, and two tenants'
    customers sharing a phone number would collide on a global unique index.
    Merging or deleting to make that fit would silently destroy one tenant's
    data, so the downgrade raises instead -- and this asserts, at the source
    level, that no data statement was ever added to make it "work".
    """
    module = _migration(TENANT_OWNERSHIP)
    source = inspect.getsource(module.downgrade)
    assert "RuntimeError" in source
    assert "TENANT_IDS_IN_USE" in source
    assert "DELETE FROM" not in source
    assert "UPDATE " not in source


def test_the_tenant_migration_exposes_its_sql_for_testing() -> None:
    """The 0013 precedent: tests drive the real statements, not copies of them."""
    module = _migration(TENANT_OWNERSHIP)
    for name in (
        "BACKFILL_USERS",
        "BACKFILL_CONVERSATIONS",
        "BACKFILL_MESSAGES",
        "BACKFILL_DOCUMENTS",
        "BACKFILL_DOCUMENT_CHUNKS",
        "BACKFILL_AI_LOGS_VIA_CONVERSATION",
        "BACKFILL_AI_LOGS_DETACHED",
        "BACKFILL_ANALYTICS_DAILY",
        "UNBACKFILLED_ROWS",
        "POPULATED_OWNED_TABLES",
        "TENANT_IDS_IN_USE",
    ):
        assert getattr(module, name, "").strip(), f"{name} missing or empty"


# --- What landed in the database --------------------------------------------


async def test_core_tables_exist_after_upgrade(db: AsyncSession) -> None:
    for table in CORE_TABLES:
        exists = await db.scalar(
            text("SELECT to_regclass(:name) IS NOT NULL"), {"name": table}
        )
        assert exists, f"table {table} missing after alembic upgrade head"


async def test_chat_sessions_table_is_gone(db: AsyncSession) -> None:
    """The model was unused, so it was removed rather than migrated."""
    exists = await db.scalar(text("SELECT to_regclass('chat_sessions') IS NOT NULL"))
    assert not exists


async def test_concurrency_and_search_objects_exist(db: AsyncSession) -> None:
    """The objects the race-condition and search fixes depend on."""
    partial_unique = await db.scalar(
        text("SELECT to_regclass('uq_active_conversation_per_user') IS NOT NULL")
    )
    assert partial_unique, "partial unique index for active conversations missing"

    trigram_index = await db.scalar(
        text("SELECT to_regclass('ix_messages_content_trgm') IS NOT NULL")
    )
    assert trigram_index, "pg_trgm GIN index on messages.content missing"

    extension = await db.scalar(
        text("SELECT count(*) FROM pg_extension WHERE extname = 'pg_trgm'")
    )
    assert extension == 1


async def test_external_id_is_not_null_after_upgrade(db: AsyncSession) -> None:
    """The contract step actually applied."""
    assert await db.scalar(text(_IS_NULLABLE)) == "NO"


async def test_both_identity_indexes_survive_the_contract_step(
    db: AsyncSession,
) -> None:
    """wa_id keeps its own unique index alongside the pair constraint.

    The phone number is still WhatsApp's identifier and still what operators
    search by, so 0013 tightens external_id without taking anything away.

    Both names survive 0016 as well, which is why that migration recreates
    them under the same names rather than introducing new ones: ix_users_wa_id
    keeps the lookup and loses only its uniqueness, and
    uq_users_channel_external_id stays a constraint because the writer names it
    in ON CONFLICT ON CONSTRAINT.
    """
    for name in ("uq_users_channel_external_id", "ix_users_wa_id"):
        exists = await db.scalar(
            text("SELECT to_regclass(:name) IS NOT NULL"), {"name": name}
        )
        assert exists, f"{name} missing after alembic upgrade head"


async def test_no_row_survives_without_the_id_it_is_addressed_by(
    db: AsyncSession,
) -> None:
    """The invariant the backfill and the writer maintain between them."""
    orphans = await db.scalar(
        text("SELECT count(*) FROM users WHERE external_id IS NULL")
    )
    assert orphans == 0

    mismatched = await db.scalar(
        text(
            "SELECT count(*) FROM users "
            "WHERE channel = 'whatsapp' AND wa_id IS NOT NULL "
            "AND external_id <> wa_id"
        )
    )
    assert mismatched == 0


# --- 0013's backfill, run for real ------------------------------------------
#
# Each of these drops the NOT NULL constraint inside its own transaction and
# rolls back in a finally, so the schema CI migrated is unchanged afterwards
# whether the assertions pass or not.


async def test_the_backfill_copies_wa_id_into_an_empty_external_id(
    db: AsyncSession,
) -> None:
    legacy_wa_id = new_wa_id()
    messenger_id = "psid-" + new_wa_id()
    backfill = _migration(EXTERNAL_ID_NOT_NULL).BACKFILL

    try:
        await db.execute(text(_DROP_NOT_NULL))
        await db.execute(text(_INSERT_LEGACY_ROW), {"wa_id": legacy_wa_id})
        await db.execute(text(_INSERT_MESSENGER_ROW), {"external_id": messenger_id})

        await db.execute(text(backfill))

        filled = await db.scalar(
            text("SELECT external_id FROM users WHERE wa_id = :wa_id"),
            {"wa_id": legacy_wa_id},
        )
        assert filled == legacy_wa_id

        # A row that already has an id must be left alone. Overwriting a PSID
        # from a NULL wa_id would unaddress a Messenger customer entirely.
        untouched = await db.scalar(
            text("SELECT external_id FROM users WHERE external_id = :id"),
            {"id": messenger_id},
        )
        assert untouched == messenger_id
    finally:
        await db.rollback()


async def test_a_row_with_no_ids_at_all_stops_the_migration(
    db: AsyncSession,
) -> None:
    """The documented hazard, made explicit rather than left in a comment.

    Such a row describes a customer the platform cannot reach on any channel.
    0013 refuses to invent an id for it, so SET NOT NULL aborts and the deploy
    fails loudly. That is the right outcome -- but only useful if whoever runs
    the migration knows to look for the row first.
    """
    try:
        await db.execute(text(_DROP_NOT_NULL))
        await db.execute(text(_INSERT_UNREACHABLE_ROW))
        await db.execute(text(_migration(EXTERNAL_ID_NOT_NULL).BACKFILL))

        still_empty = await db.scalar(
            text("SELECT count(*) FROM users WHERE external_id IS NULL")
        )
        assert still_empty == 1

        with pytest.raises(IntegrityError):
            await db.execute(text(_SET_NOT_NULL))
    finally:
        await db.rollback()


async def test_the_downgraded_column_accepts_a_legacy_row_again(
    db: AsyncSession,
) -> None:
    """Schema-level reversibility, without touching the data.

    Runs the same ALTER the downgrade runs and checks that a row shaped like
    the ones 0009 produced is accepted once more.
    """
    try:
        await db.execute(text(_DROP_NOT_NULL))
        await db.execute(text(_INSERT_LEGACY_ROW), {"wa_id": new_wa_id()})
        assert await db.scalar(text(_IS_NULLABLE)) == "YES"
    finally:
        await db.rollback()
