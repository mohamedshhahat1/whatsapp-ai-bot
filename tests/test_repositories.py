"""Regression: concurrent webhook deliveries must not duplicate rows.

Two messages sent in quick succession produce two deliveries that two workers
can process at the same time. The old SELECT-then-INSERT lost that race: one
worker died on the unique index, or -- worse, because it was silent -- two
active conversations were created for one customer and the model was shown
half the history.
"""

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import SessionLocal
from app.models.conversation import Conversation
from app.repositories.conversation import ConversationRepository
from app.repositories.user import UserRepository
from tests.conftest import Customer, new_wa_id, purge


async def test_get_or_create_user_is_idempotent(db: AsyncSession) -> None:
    wa_id = new_wa_id()
    users = UserRepository(db)
    try:
        first = await users.get_or_create(wa_id=wa_id, name="First")
        await db.commit()
        second = await users.get_or_create(wa_id=wa_id, name="First")
        await db.commit()
        assert first.id == second.id
    finally:
        await purge(db, wa_id)


async def test_get_or_create_user_survives_a_competing_insert(
    db: AsyncSession,
) -> None:
    """The loser of the race re-reads the winner's row instead of raising."""
    wa_id = new_wa_id()
    try:
        async with SessionLocal() as other:
            winner = await UserRepository(other).get_or_create(wa_id=wa_id)
            await other.commit()
            winner_id = winner.id

        loser = await UserRepository(db).get_or_create(wa_id=wa_id, name="Late")
        await db.commit()
        assert loser.id == winner_id
    finally:
        await purge(db, wa_id)


async def test_get_or_create_active_conversation_is_idempotent(
    db: AsyncSession, customer: Customer
) -> None:
    conversations = ConversationRepository(db)
    again = await conversations.get_or_create_active(customer.user_id)
    await db.commit()
    assert again.id == customer.conversation_id


async def test_database_rejects_a_second_active_conversation(
    db: AsyncSession, customer: Customer
) -> None:
    """The guarantee is the partial unique index, not application code.

    Uses its own session because the failed transaction has to be rolled
    back, which would otherwise discard the fixture's work.
    """
    async with SessionLocal() as other:
        other.add(Conversation(user_id=customer.user_id, status="active"))
        with pytest.raises(IntegrityError):
            await other.commit()
        await other.rollback()


async def test_closed_conversations_do_not_block_a_new_one(
    db: AsyncSession, customer: Customer
) -> None:
    """The index is partial: only one *active* conversation is restricted."""
    db.add(Conversation(user_id=customer.user_id, status="closed"))
    await db.commit()
    still_open = await ConversationRepository(db).get_or_create_active(
        customer.user_id
    )
    assert still_open.id == customer.conversation_id
