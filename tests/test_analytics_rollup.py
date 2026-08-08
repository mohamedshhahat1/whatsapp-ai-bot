"""Nightly analytics rollup: first run, idempotency, boundaries, empty days.

Every database test pins its data to a fixed calendar day in the distant past.
Rolling up "yesterday" would race every other test in the suite that writes a
message, and the assertions would then depend on what else had run.

The AI logs are recorded against a model name that has no row in
model_pricing, so the pricing LATERAL finds nothing and cost falls back to the
defaults passed in. That makes the expected spend computable by hand instead
of depending on whatever migration 0002 seeded.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, update
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


def _add_log(
    db: AsyncSession,
    *,
    when: datetime,
    prompt: int = 100,
    completion: int = 50,
    latency: int = 200,
    error: str | None = None,
) -> None:
    db.add(
        AILog(
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
    db: AsyncSession, customer: Customer
) -> None:
    _add_log(db, when=datetime(2001, 3, 4, 9, 0, tzinfo=UTC))
    _add_log(
        db,
        when=datetime(2001, 3, 4, 17, 0, tzinfo=UTC),
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


async def test_rerunning_the_same_night_does_not_duplicate(db: AsyncSession) -> None:
    _add_log(db, when=datetime(2001, 3, 4, 12, 0, tzinfo=UTC))
    await db.commit()

    rollup = AnalyticsRollupRepository(db)
    await rollup.rollup_day(DAY, DEFAULTS)
    await db.commit()
    await rollup.rollup_day(DAY, DEFAULTS)
    await db.commit()

    db.expire_all()
    rows = (
        (await db.execute(select(AnalyticsDaily).where(AnalyticsDaily.day == DAY)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].requests == 1


async def test_rerunning_picks_up_rows_that_arrived_late(db: AsyncSession) -> None:
    """The conflict action must be DO UPDATE, not DO NOTHING.

    A re-run exists precisely to catch rows written after the first attempt.
    DO NOTHING would skip the day for having been seen, and the rollup would
    silently under-report forever.
    """
    _add_log(db, when=datetime(2001, 3, 4, 1, 0, tzinfo=UTC))
    await db.commit()

    rollup = AnalyticsRollupRepository(db)
    await rollup.rollup_day(DAY, DEFAULTS)
    await db.commit()

    _add_log(db, when=datetime(2001, 3, 4, 2, 0, tzinfo=UTC))
    await db.commit()
    await rollup.rollup_day(DAY, DEFAULTS)
    await db.commit()

    db.expire_all()
    row = await rollup.get(DAY)
    assert row is not None
    assert row.requests == 2


async def test_midnight_belongs_to_exactly_one_day(db: AsyncSession) -> None:
    """The range is half-open, so 00:00:00 starts a day rather than ending one."""
    _add_log(db, when=datetime(2001, 3, 4, 23, 59, 59, tzinfo=UTC))
    _add_log(db, when=datetime(2001, 3, 5, 0, 0, 0, tzinfo=UTC))
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


async def test_a_day_with_no_activity_is_stored_as_zeros(db: AsyncSession) -> None:
    """ "Rolled up, nothing happened" must be distinguishable from "never run".

    The aggregates are bare, so they return one row even over an empty range.
    """
    empty_day = date(2001, 1, 1)
    rollup = AnalyticsRollupRepository(db)
    await rollup.rollup_day(empty_day, DEFAULTS)
    await db.commit()

    row = await rollup.get(empty_day)
    assert row is not None
    assert row.requests == 0
    assert row.errors == 0
    assert row.total_tokens == 0
    assert row.messages == 0
    assert row.input_cost_usd == Decimal("0")
    assert row.avg_latency_ms == 0.0


async def test_rollup_days_processes_each_day_given(db: AsyncSession) -> None:
    _add_log(db, when=datetime(2001, 3, 4, 6, 0, tzinfo=UTC))
    _add_log(db, when=datetime(2001, 3, 5, 6, 0, tzinfo=UTC))
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
