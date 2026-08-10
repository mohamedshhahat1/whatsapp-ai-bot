"""Writing customers after 0013: one row per (channel, external_id).

The migration and UserRepository are only correct together. 0013 made
``users.external_id`` NOT NULL, and the WhatsApp writer had to start filling
it in the same commit -- so these drive the writer against the migrated
schema rather than against the model. 0016 did the same thing again with
``tenant_id``, which is why the hand-written inserts below name it too.

The race these ON CONFLICT clauses exist for is real: a customer who sends two
messages quickly produces two webhook deliveries that two Celery workers can
process at once.
"""

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.constants import MESSENGER, WHATSAPP
from app.models.user import User
from app.repositories.user import UserRepository
from tests.conftest import new_wa_id, purge

_DELETE_BY_CHANNEL_ID = (
    "DELETE FROM users WHERE channel = :channel AND external_id = :external_id"
)


async def _purge_channel(
    session: AsyncSession,
    channel: str,
    external_id: str,
) -> None:
    """Remove a non-WhatsApp customer.

    conftest.purge keys every statement on wa_id, which these rows do not
    have, so they would otherwise accumulate across runs.
    """
    await session.execute(
        text(_DELETE_BY_CHANNEL_ID),
        {"channel": channel, "external_id": external_id},
    )
    await session.commit()


async def _count_wa(session: AsyncSession, wa_id: str) -> int:
    total = await session.scalar(select(func.count(User.id)).where(User.wa_id == wa_id))
    return int(total or 0)


# --- WhatsApp ---------------------------------------------------------------


async def test_a_whatsapp_customer_is_written_with_both_ids(
    db: AsyncSession,
) -> None:
    """external_id is not a placeholder here.

    On WhatsApp the phone number IS the platform's own id for the customer, so
    copying it records the same fact under the name every channel uses.
    """
    wa_id = new_wa_id()
    try:
        user = await UserRepository(db).get_or_create(wa_id, name="Test User")
        await db.commit()
        assert user.channel == WHATSAPP
        assert user.external_id == wa_id
        assert user.wa_id == wa_id
    finally:
        await purge(db, wa_id)


async def test_writing_the_same_whatsapp_customer_twice_is_one_row(
    db: AsyncSession,
) -> None:
    wa_id = new_wa_id()
    try:
        repo = UserRepository(db)
        first = await repo.get_or_create(wa_id, name="Test User")
        await db.commit()
        second = await repo.get_or_create(wa_id, name="Test User")
        await db.commit()
        assert first.id == second.id
        assert await _count_wa(db, wa_id) == 1
    finally:
        await purge(db, wa_id)


async def test_the_losing_writer_of_a_race_is_a_no_op(
    db: AsyncSession, default_tenant: int
) -> None:
    """What ON CONFLICT DO NOTHING buys, after the schema change.

    Driven at SQL level because the race cannot be staged deterministically
    through the repository: get_or_create re-SELECTs, and by the time a second
    writer could have committed, that SELECT finds the row. Racing two live
    sessions instead would block on the unique index until one committed. This
    is the exact statement the loser runs, against a row already committed.

    The tenant is part of that statement since 0016 -- get_or_create fills it
    in -- and without it the insert would fail NOT NULL before ON CONFLICT got
    the chance to decide anything.
    """
    wa_id = new_wa_id()
    try:
        await UserRepository(db).get_or_create(wa_id, name="Winner")
        await db.commit()

        await db.execute(
            pg_insert(User)
            .values(
                tenant_id=default_tenant,
                channel=WHATSAPP,
                external_id=wa_id,
                wa_id=wa_id,
                name="Loser",
            )
            .on_conflict_do_nothing()
        )
        await db.commit()

        assert await _count_wa(db, wa_id) == 1
    finally:
        await purge(db, wa_id)


async def test_naming_one_constraint_would_leave_the_other_unguarded(
    db: AsyncSession, default_tenant: int
) -> None:
    """Why get_or_create leaves its conflict target unnamed.

    A duplicate customer row can trip either unique constraint, and Postgres
    reports whichever it reaches first. Naming uq_users_channel_external_id
    leaves the phone-number uniqueness free to raise IntegrityError -- the
    exact failure the clause exists to prevent, and one that would only appear
    under load. Since 0016 that second constraint is uq_users_tenant_wa_id
    rather than the global ix_users_wa_id: scoping it to the tenant changed
    which rows collide, not that they collide.
    """
    wa_id = new_wa_id()
    try:
        await UserRepository(db).get_or_create(wa_id, name="Existing")
        await db.commit()

        # Same phone number, different channel: the pair constraint is not
        # violated at all, so naming it guards nothing here. The tenant
        # matches the existing row, which leaves the phone number as the only
        # thing this insert can collide on.
        with pytest.raises(IntegrityError):
            await db.execute(
                pg_insert(User)
                .values(
                    tenant_id=default_tenant,
                    channel=MESSENGER,
                    external_id="psid-" + wa_id,
                    wa_id=wa_id,
                    name="Different channel",
                )
                .on_conflict_do_nothing(constraint="uq_users_channel_external_id")
            )
        await db.rollback()

        assert await _count_wa(db, wa_id) == 1
    finally:
        await purge(db, wa_id)


# --- Other channels ---------------------------------------------------------


async def test_a_messenger_customer_is_written_without_a_phone_number(
    db: AsyncSession,
) -> None:
    external_id = "psid-" + new_wa_id()
    try:
        user = await UserRepository(db).get_or_create_by_channel(
            MESSENGER, external_id, name="Messenger User"
        )
        await db.commit()
        assert user.channel == MESSENGER
        assert user.external_id == external_id
        assert user.wa_id is None
    finally:
        await _purge_channel(db, MESSENGER, external_id)


async def test_writing_the_same_messenger_customer_twice_is_one_row(
    db: AsyncSession,
) -> None:
    """Before 0013 this was not true.

    external_id was NULL for every row get_or_create wrote, and Postgres
    treats each NULL in a unique index as distinct, so the pair constraint
    never fired and two workers could give one customer two identities.
    """
    external_id = "psid-" + new_wa_id()
    try:
        repo = UserRepository(db)
        first = await repo.get_or_create_by_channel(MESSENGER, external_id)
        await db.commit()
        second = await repo.get_or_create_by_channel(MESSENGER, external_id)
        await db.commit()
        assert first.id == second.id
    finally:
        await _purge_channel(db, MESSENGER, external_id)


async def test_the_same_id_on_two_channels_is_two_customers(
    db: AsyncSession,
) -> None:
    """Meta exposes no way to match a PSID to a phone number.

    One row carrying several ids would need a merge rule that could only ever
    be a guess, and a wrong guess shows one customer another's history.
    """
    shared = new_wa_id()
    try:
        repo = UserRepository(db)
        on_whatsapp = await repo.get_or_create(shared, name="On WhatsApp")
        await db.commit()
        on_messenger = await repo.get_or_create_by_channel(
            MESSENGER, shared, name="On Messenger"
        )
        await db.commit()

        assert on_whatsapp.id != on_messenger.id
        assert on_whatsapp.external_id == on_messenger.external_id
        assert on_whatsapp.channel != on_messenger.channel
    finally:
        await _purge_channel(db, MESSENGER, shared)
        await purge(db, shared)
