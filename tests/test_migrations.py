"""C2 regression: a fresh database must be usable after `alembic upgrade head`.

The repository previously had no baseline migration, so the core tables only
existed if someone ran `alembic revision --autogenerate` by hand on the
server. The first group of tests inspects the migration graph; the second
checks what actually landed in the database CI migrated before pytest ran.
"""

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import REPOSITORY_ROOT_MARKER  # noqa: F401  (see below)

CORE_TABLES = ("users", "conversations", "messages", "ai_logs")


def _scripts() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config("alembic.ini"))


def test_migration_history_has_a_single_head() -> None:
    """Two heads would make `alembic upgrade head` ambiguous and fail."""
    assert len(_scripts().get_heads()) == 1


def test_migration_history_is_a_single_chain_from_the_baseline() -> None:
    revisions = list(_scripts().walk_revisions())
    # Exactly one revision may be the base (down_revision is None).
    bases = [r.revision for r in revisions if r.down_revision is None]
    assert bases == ["0000_initial_schema"]
    assert len(revisions) >= 4


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
