"""Audit retention expires old records without weakening append-only.

The cutoff is passed explicitly and set in the future, so these tests never
have to fabricate a created_at or wait for one to age. Assertions are about
the specific rows each test created rather than about totals, because other
tests in the suite write audit rows too.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.retention_config import RetentionSettings
from app.models.audit_log import AuditLog
from app.services.audit_service import AuditService


async def _record(db: AsyncSession, marker: str) -> int:
    entry = await AuditService(db).record(
        None,
        "conversation.delete",
        resource_type="system",
        resource_id=marker,
    )
    return int(entry.id)


async def _exists(db: AsyncSession, entry_id: int) -> bool:
    found = await db.execute(select(AuditLog.id).where(AuditLog.id == entry_id))
    return found.scalar_one_or_none() is not None


def test_retention_defaults_to_a_year() -> None:
    settings = RetentionSettings()
    assert settings.audit_retention_days == 365
    assert settings.audit_retention == timedelta(days=365)
    assert settings.enforced


def test_zero_days_disables_expiry() -> None:
    """The escape hatch for anyone whose answer is 'never delete'."""
    settings = RetentionSettings(audit_retention_days=0)
    assert not settings.enforced


async def test_rows_older_than_the_cutoff_are_deleted(
    db: AsyncSession, requires_database: None
) -> None:
    first = await _record(db, "retention-old-1")
    second = await _record(db, "retention-old-2")
    cutoff = datetime.now(UTC) + timedelta(hours=1)

    removed = await AuditService(db).purge_older_than(cutoff)

    assert removed >= 2
    assert not await _exists(db, first)
    assert not await _exists(db, second)


async def test_rows_newer_than_the_cutoff_survive(
    db: AsyncSession, requires_database: None
) -> None:
    entry_id = await _record(db, "retention-fresh")
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    try:
        assert await AuditService(db).purge_older_than(cutoff) == 0
        assert await _exists(db, entry_id)
    finally:
        await AuditService(db).purge_older_than(datetime.now(UTC) + timedelta(hours=1))


async def test_the_purge_stops_at_its_batch_ceiling(
    db: AsyncSession, requires_database: None
) -> None:
    """One sweep is bounded, so a huge backlog cannot occupy a worker."""
    await _record(db, "retention-batch-1")
    await _record(db, "retention-batch-2")
    cutoff = datetime.now(UTC) + timedelta(hours=1)

    removed = await AuditService(db).purge_older_than(
        cutoff, batch_size=1, max_batches=1
    )

    assert removed == 1
    # The remainder is still there for the next tick to take.
    await AuditService(db).purge_older_than(cutoff)


async def test_update_is_still_categorically_blocked(
    db: AsyncSession, requires_database: None
) -> None:
    """Retention expires records. It must never permit rewriting one."""
    entry_id = await _record(db, "retention-immutable")
    try:
        with pytest.raises(DBAPIError):
            await db.execute(
                update(AuditLog)
                .where(AuditLog.id == entry_id)
                .values(action="tampered")
            )
        await db.rollback()
    finally:
        await AuditService(db).purge_older_than(datetime.now(UTC) + timedelta(hours=1))


async def test_a_delete_without_the_flag_is_still_refused(
    db: AsyncSession, requires_database: None
) -> None:
    """The exemption is unreachable from an ordinary transaction.

    A compromised application role must not be able to remove yesterday's
    evidence just because a retention path exists somewhere in the codebase.
    """
    entry_id = await _record(db, "retention-guarded")
    try:
        with pytest.raises(DBAPIError):
            await db.execute(delete(AuditLog).where(AuditLog.id == entry_id))
        await db.rollback()
        assert await _exists(db, entry_id)
    finally:
        await AuditService(db).purge_older_than(datetime.now(UTC) + timedelta(hours=1))
