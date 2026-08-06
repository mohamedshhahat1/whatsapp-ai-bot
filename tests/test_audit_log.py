"""The audit log, and the fact that it cannot be rewritten.

These tests leave their rows behind. That is not untidiness: the table
blocks DELETE, which is the property under test, so there is nothing to
clean up. They are attributed to the seeded legacy operator rather than a
throwaway account for the same reason -- audit_logs.operator_id is ON DELETE
RESTRICT, so an operator with rows cannot be removed either, and a fixture
that tried to would fail in teardown.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import (
    ACTION_AI_TOGGLE,
    ACTION_CONVERSATION_DELETE,
    RESOURCE_CONVERSATION,
    RESOURCE_SYSTEM,
)
from app.services.audit_service import AuditService


async def test_recording_an_action_attributes_it_to_an_operator(
    db: AsyncSession,
) -> None:
    """None means the shared key, and resolves to the reserved operator.

    This is what lets operator_id be NOT NULL without withdrawing a
    credential the mobile client still depends on.
    """
    entry = await AuditService(db).record(
        None,
        ACTION_AI_TOGGLE,
        resource_type=RESOURCE_SYSTEM,
        details={"enabled": False},
    )
    assert entry.id is not None
    assert entry.operator_id is not None
    assert entry.action == ACTION_AI_TOGGLE
    assert entry.details == {"enabled": False}
    assert entry.created_at is not None


async def test_the_resource_id_is_stored_as_text(db: AsyncSession) -> None:
    """One column holds conversation ids, wa_ids and model names alike."""
    entry = await AuditService(db).record(
        None,
        ACTION_CONVERSATION_DELETE,
        resource_type=RESOURCE_CONVERSATION,
        resource_id=4321,
    )
    assert entry.resource_id == "4321"


async def test_audit_rows_cannot_be_updated(db: AsyncSession) -> None:
    """Application discipline is not immutability; the database enforces it."""
    entry = await AuditService(db).record(
        None, ACTION_AI_TOGGLE, resource_type=RESOURCE_SYSTEM
    )
    with pytest.raises(DBAPIError):
        await db.execute(
            text("UPDATE audit_logs SET action = 'tampered' WHERE id = :id"),
            {"id": entry.id},
        )
        await db.commit()
    await db.rollback()


async def test_audit_rows_cannot_be_deleted(db: AsyncSession) -> None:
    entry = await AuditService(db).record(
        None, ACTION_AI_TOGGLE, resource_type=RESOURCE_SYSTEM
    )
    with pytest.raises(DBAPIError):
        await db.execute(
            text("DELETE FROM audit_logs WHERE id = :id"), {"id": entry.id}
        )
        await db.commit()
    await db.rollback()


async def test_actions_can_be_read_back_for_one_resource(db: AsyncSession) -> None:
    audit = AuditService(db)
    await audit.record(
        None,
        ACTION_CONVERSATION_DELETE,
        resource_type=RESOURCE_CONVERSATION,
        resource_id=999_001,
    )
    found = await audit.list_for_resource(RESOURCE_CONVERSATION, 999_001)
    assert len(found) >= 1
    assert all(entry.resource_id == "999001" for entry in found)
