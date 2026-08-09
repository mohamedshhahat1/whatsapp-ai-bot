"""Nightly analytics rollup: first run, idempotency, boundaries, empty days.

Every database test pins its data to a fixed calendar day in the distant past.
Rolling up "yesterday" would race every other test in the suite that writes a
message, and the assertions would then depend on what else had run.

The AI logs are recorded against a model name that has no row in
model_pricing, so the pricing LATERAL finds nothing and cost falls back to the
defaults passed in. That makes the expected spend computable by hand instead
of depending on whatever migration 0002 seeded.

Since 0016 the rollup writes one row per tenant per day, so these tests name
the tenant they are asserting about rather than assuming the table holds a
single row. Cross-tenant separation is covered in test_tenant_ownership.py,
which has the fixture for a second tenant.

Cleaning up is this module's own responsibility, unlike everywhere else in the
suite. ``_add_log`` writes rows whose ``conversation_id`` is NULL, because the
rollup counts API calls rather than conversations, and both cleanup helpers in
conftest delete ai_logs by joining through a customer's conversations. Neither
``purge`` nor ``purge_channel`` can reach a row that hangs off no conversation,
so the ``rollup_tables`` fixture below removes them by model name instead.
"""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_log import AILog
from app.models.analytics_rollup import AnalyticsDaily
from app.models.message import Message
from app.repositories.analytics import PriceDefaults
from app.repositories.analytics_rollup import (
    AnalyticsRollupRepository,
    complete_days_before,
    day_bounds,
)
from app.repositories.message import MessageRepository
from tests.conftest import Customer

UNPRICED_MODEL = "test-rollup-model"

# $1 per million input tokens, $2 per million output tokens. Round numbers so
# the arithmetic in the assertions is readable.
DEFAULTS = PriceDefaults(input_price=Decimal("1"), output_price=Decimal("2"))

DAY = date(2001, 3, 4)
NEXT_DAY = date(2001, 3, 5)
EMPTY_DAY = date(2001, 1, 1)

# Every day this module rolls up, so the fixture can clear the stored summaries
# as well as the logs they were computed from.
ROLLED_DAYS = (DAY, NEXT_DAY, EMPTY_DAY)


def _add_log(
    db: AsyncSession,
    *,
    when: datetime,
    tenant_id: int,
    prompt: int = 100,
    completion: int = 50,
    latency: int = 200,
    error: str | None = None,
) -> None:
    """Add one usage row directly, bypassing the repository.

    ``tenant_id`` is required rather than defaulted. These rows are built by
    hand, so nothing would resolve a fallback for them -- and a test that
    guessed the tenant would be asserting about a row it had not placed.
    """
    db.add(
        AILog(
            tenant_id=tenant_id,
            conversation_id=None,
            model=UNPRICED_MODEL,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            latency_ms=latency,
            error=error,
            created_at=when,
        )
    )


async def _add_message_at(
    db: AsyncSession, customer: Customer, when: datetime, marker: str
) -> None:
    """Create a message and then move it to a chosen instant.

    Written in two steps because created_at is server-defaulted; updating it
    afterwards touches only columns this test can see, rather than depending
    on the shape of the Message constructor.
    """
    await MessageRepository(db).create(
        conversation_id=customer.conversation_id,
        direction="inbound",
        content=marker,
    )
    await db.commit()
    await db.execute(
        update(Message).where(Message.content == marker).values(created_at=when)
    )
    await db.commit()


async def _clear_rollup_rows(session: AsyncSession) -> None:
    """Delete only what this module writes.

    ``model`` is the discriminator rather than a date range: UNPRICED_MODEL is
    unique to this file, so the delete cannot reach a log another test owns
    even if that test picked the same day.

    The stored summaries are cleared for every tenant, not just the default
    one. The rollup writes a row per tenant, so leaving another tenant's row
    behind would both break the counts here and block that tenant's teardown
    -- analytics_daily references tenants ON DELETE RESTRICT.
    """
    await session.execute(delete(AILog).where(AILog.model == UNPRICED_MODEL))
    await session.execute(
        delete(AnalyticsDaily).where(AnalyticsDaily.day.in_(ROLLED_DAYS))
    )
    await session.commit()


@pytest.fixture
async def rollup_tables(db: AsyncSession) -> AsyncIterator[None]:
    """Give each database test an empty slate to count against.

    Before as well as after. Clearing only on teardown would still leave the
    first test of a run exposed to rows a previous, interrupted run left
    behind, which is the same failure one process later.
    """
    await _clear_rollup_rows(db)
    try:
        yield
    finally:
        await _clear_rollup_rows(db)


def test_day_bounds_is_half_open_and_anchored_to_utc() -> None:
    start, end = day_bounds(DAY)
    assert start == datetime(2001, 3, 4, tzinfo=UTC)
    assert end == datetime(2001, 3, 5, tzinfo=UTC)
    assert end - start == timedelta(days=1)


def test_complete_days_before_never_includes_today() -> None:
    """Today is still accumulating; storing it would record a partial day."""
    now = datetime(2026, 8, 8, 0, 20, tzinfo=UTC)
    assert complete_days_before(now, 2) == [date(2026, 8, 6), date(2026, 8, 7)]


def test_complete_days_before_is_oldest_first() -> None:
    now = datetime(2026, 8, 8, 0, 20, tzinfo=UTC)
    days = complete_days_before(now, 3)
    assert days == sorted(days)


def test_complete_days_before_converts_to_utc_before_taking_the_date() -> None:
    """A local clock already past midnight does not advance the UTC day."""
    cairo_just_after_midnight = datetime(2026, 8, 9, 0, 30, tzinfo=UTC) - timedelta(
        hours=3
    )
    assert complete_days_before(cairo_just_after_midnight, 1) == [date(2026, 8, 7)]


async def test_first_rollup_summarises_the_day(
    db: AsyncSession,
    customer: Customer,
    default_tenant: int,
    rollup_tables: None,
) -> None:
    _add_log(db, when=datetime(2001, 3, 4, 9, 0, tzinfo=UTC), tenant_id=default_tenant)
    _add_log(
        db,
        when=datetime(2001, 3, 4, 17, 0, tzinfo=UTC),
        tenant_id=default_tenant,
        latency=400,
        error="upstream timeout",
    )
    await db.commit()
    await _add_message_at(
        db,
        customer,
        datetime(2001, 3, 4, 9, 1, tzinfo=UTC),
        f"rollup-first-{customer.wa_id}",
    )

    rollup = AnalyticsRollupRepository(db)
    await rollup.rollup_day(DAY, DEFAULTS)
    await db.commit()

    row = await rollup.get(DAY)
    assert row is not None
    assert row.tenant_id == default_tenant
    assert row.requests == 2
    # count() over the nullable error column counts failures only.
    assert row.errors == 1
    assert row.prompt_tokens == 200
    assert row.completion_tokens == 100
    assert row.total_tokens == 300
    assert row.latency_ms_sum == 600
    assert row.avg_latency_ms == 300.0
    # 200 input tokens at $1/1M and 100 output tokens at $2/1M.
    assert row.input_cost_usd == Decimal("0.000200")
    assert row.output_cost_usd == Decimal("0.000200")
    assert row.cost_usd == Decimal("0.000400")
    assert row.messages == 1


async def test_rerunning_the_same_night_does_not_duplicate(
    db: AsyncSession, default_tenant: int, rollup_tables: None
) -> None:
    _add_log(db, when=datetime(2001, 3, 4, 12, 0, tzinfo=UTC), tenant_id=default_tenant)
    await db.commit()

    rollup = AnalyticsRollupRepository(db)
    await rollup.rollup_day(DAY, DEFAULTS)
    await db.commit()
    await rollup.rollup_day(DAY, DEFAULTS)
    await db.commit()

    db.expire_all()
    rows = (
        (
            await db.execute(
                select(AnalyticsDaily).where(
                    AnalyticsDaily.day == DAY,
                    AnalyticsDaily.tenant_id == default_tenant,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].requests == 1


async def test_rerunning_picks_up_rows_that_arrived_late(
    db: AsyncSession, default_tenant: int, rollup_tables: None
) -> None:
    """The conflict action must be DO UPDATE, not DO NOTHING.

    A re-run exists precisely to catch rows written after the first attempt.
    DO NOTHING would skip the day for having been seen, and the rollup would
    silently under-report forever.
    """
    _add_log(db, when=datetime(2001, 3, 4, 1, 0, tzinfo=UTC), tenant_id=default_tenant)
    await db.commit()

    rollup = AnalyticsRollupRepository(db)
    await rollup.rollup_day(DAY, DEFAULTS)
    await db.commit()

    _add_log(db, when=datetime(2001, 3, 4, 2, 0, tzinfo=UTC), tenant_id=default_tenant)
    await db.commit()
    await rollup.rollup_day(DAY, DEFAULTS)
    await db.commit()

    db.expire_all()
    row = await rollup.get(DAY)
    assert row is not None
    assert row.requests == 2


async def test_midnight_belongs_to_exactly_one_day(
    db: AsyncSession, default_tenant: int, rollup_tables: None
) -> None:
    """The range is half-open, so 00:00:00 starts a day rather than ending one."""
    _add_log(
        db,
        when=datetime(2001, 3, 4, 23, 59, 59, tzinfo=UTC),
        tenant_id=default_tenant,
    )
    _add_log(
        db, when=datetime(2001, 3, 5, 0, 0, 0, tzinfo=UTC), tenant_id=default_tenant
    )
    await db.commit()

    rollup = AnalyticsRollupRepository(db)
    await rollup.rollup_day(DAY, DEFAULTS)
    await rollup.rollup_day(NEXT_DAY, DEFAULTS)
    await db.commit()

    db.expire_all()
    earlier = await rollup.get(DAY)
    later = await rollup.get(NEXT_DAY)
    assert earlier is not None and later is not None
    assert earlier.requests == 1
    assert later.requests == 1


async def test_a_day_with_no_activity_is_stored_as_zeros(
    db: AsyncSession, rollup_tables: None
) -> None:
    """ "Rolled up, nothing happened" must be distinguishable from "never run".

    This is why the statement is driven FROM tenants and LEFT JOINs the
    aggregates rather than grouping them by tenant_id. A grouped aggregate over
    an empty range returns no rows at all, so an idle day would leave a gap
    indistinguishable from a scheduler that never ran.
    """
    rollup = AnalyticsRollupRepository(db)
    await rollup.rollup_day(EMPTY_DAY, DEFAULTS)
    await db.commit()

    row = await rollup.get(EMPTY_DAY)
    assert row is not None
    assert row.requests == 0
    assert row.errors == 0
    assert row.total_tokens == 0
    assert row.messages == 0
    assert row.input_cost_usd == Decimal("0")
    assert row.avg_latency_ms == 0.0


async def test_rollup_days_processes_each_day_given(
    db: AsyncSession, default_tenant: int, rollup_tables: None
) -> None:
    _add_log(db, when=datetime(2001, 3, 4, 6, 0, tzinfo=UTC), tenant_id=default_tenant)
    _add_log(db, when=datetime(2001, 3, 5, 6, 0, tzinfo=UTC), tenant_id=default_tenant)
    await db.commit()

    rollup = AnalyticsRollupRepository(db)
    processed = await rollup.rollup_days([DAY, NEXT_DAY], DEFAULTS)
    await db.commit()

    assert processed == 2
    db.expire_all()
    for day in (DAY, NEXT_DAY):
        row = await rollup.get(day)
        assert row is not None, f"{day} was not rolled up"
        assert row.requests == 1
