"""Writes the nightly analytics rollup.

Kept apart from :mod:`app.repositories.analytics`, which is documented and
tested as read-only aggregates. This is the one place that writes derived
analytics, and the separation keeps that property easy to check.

The cost expressions are imported from that module rather than restated, so a
change to how a call is priced cannot make the rollup disagree with the live
query it is meant to summarise.

Day boundaries
--------------
Boundaries are computed here as UTC-aware datetimes and applied as a half-open
``[start, end)`` range, rather than derived in SQL with ``date_trunc``.

``date_trunc('day', ts)`` on a ``timestamptz`` truncates in the *session* time
zone, so the identical statement buckets rows differently depending on which
connection runs it. For a dashboard query a human is reading, that is a
curiosity. For a job that writes persistent rows it is a trap: a worker whose
connection has a different TimeZone would silently re-bucket history, and the
only symptom would be totals that no longer add up.

``AnalyticsRepository.daily_usage`` still uses ``date_trunc`` and therefore
still carries that dependency. Changing it is out of scope here, but it is
why this module does not copy the pattern.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Date, func, literal, select, true
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.ai_log import AILog
from app.models.analytics_rollup import AnalyticsDaily
from app.models.message import Message
from app.repositories.analytics import PriceDefaults, _cost_parts, _pricing_lateral
from app.repositories.base import BaseRepository

# Columns written by the rollup. "day" leads because it is the conflict target
# and is therefore never part of the update set.
ROLLUP_COLUMNS = (
    "day",
    "requests",
    "errors",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "latency_ms_sum",
    "input_cost_usd",
    "output_cost_usd",
    "messages",
)


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """The half-open UTC range ``[start, end)`` covering one calendar day.

    Half-open rather than inclusive on both ends so that midnight belongs to
    exactly one day. An inclusive upper bound would count a row logged at
    exactly 00:00:00 in both the day that ended and the day that began.
    """
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def complete_days_before(now: datetime, lookback: int) -> list[date]:
    """The ``lookback`` most recent days that have finished, oldest first.

    Today is always excluded: it is still accumulating, and rolling it up
    would store a partial figure that later looks like a real decline in
    traffic. Conversion is to UTC first, so a job that fires at 02:00 local
    time in a positive offset does not roll up a day that has not ended in
    UTC yet.
    """
    today = now.astimezone(UTC).date()
    return [today - timedelta(days=offset) for offset in range(lookback, 0, -1)]


class AnalyticsRollupRepository(BaseRepository):
    """Populates ``analytics_daily``."""

    async def rollup_day(self, day: date, defaults: PriceDefaults) -> None:
        """Recompute and store the summary for one UTC day.

        Idempotent: the day is the primary key, so a second run for the same
        date collides and updates in place. The conflict action is DO UPDATE
        rather than DO NOTHING on purpose -- a re-run exists precisely to pick
        up rows that arrived after the first attempt, and DO NOTHING would
        skip the day for having been seen.

        One statement, and one round trip. Both aggregate subqueries are bare
        (no GROUP BY), so each returns exactly one row even over an empty
        range, and joining them ON true is a one-row cross join rather than a
        fan-out. That is also what makes an empty day land as a row of zeros
        instead of no row at all.

        Both scans are range predicates on indexed created_at columns.
        """
        start, end = day_bounds(day)
        pricing = _pricing_lateral()
        input_cost, output_cost = _cost_parts(pricing, defaults)

        logs = (
            select(
                func.count(AILog.id).label("requests"),
                # count() of a nullable column counts non-NULLs only, which is
                # exactly the number of failed calls. Same definition as
                # usage_totals uses for the overview's error_rate.
                func.count(AILog.error).label("errors"),
                func.coalesce(func.sum(AILog.prompt_tokens), 0).label("prompt_tokens"),
                func.coalesce(func.sum(AILog.completion_tokens), 0).label(
                    "completion_tokens"
                ),
                func.coalesce(func.sum(AILog.total_tokens), 0).label("total_tokens"),
                func.coalesce(func.sum(AILog.latency_ms), 0).label("latency_ms_sum"),
                func.coalesce(func.sum(input_cost), 0).label("input_cost_usd"),
                func.coalesce(func.sum(output_cost), 0).label("output_cost_usd"),
            )
            .select_from(AILog)
            .outerjoin(pricing, true())
            .where(AILog.created_at >= start, AILog.created_at < end)
            .subquery()
        )

        messages = (
            select(func.count(Message.id).label("messages"))
            .where(Message.created_at >= start, Message.created_at < end)
            .subquery()
        )

        source = (
            select(
                literal(day, Date).label("day"),
                logs.c.requests,
                logs.c.errors,
                logs.c.prompt_tokens,
                logs.c.completion_tokens,
                logs.c.total_tokens,
                logs.c.latency_ms_sum,
                logs.c.input_cost_usd,
                logs.c.output_cost_usd,
                messages.c.messages,
            )
            .select_from(logs)
            .join(messages, true())
        )

        statement = pg_insert(AnalyticsDaily).from_select(
            list(ROLLUP_COLUMNS), source
        )
        updates: dict[str, Any] = {
            name: getattr(statement.excluded, name) for name in ROLLUP_COLUMNS[1:]
        }
        # Moved forward even when nothing changed, so a stalled scheduler shows
        # up as an updated_at that stops advancing.
        updates["updated_at"] = func.now()

        await self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[AnalyticsDaily.day],
                set_=updates,
            )
        )

    async def rollup_days(
        self, days: list[date], defaults: PriceDefaults
    ) -> int:
        """Roll up several days, returning how many were processed.

        Sequential rather than one combined statement. The nightly job passes
        a handful of days, so the round trips are irrelevant, and keeping each
        day its own statement means a backfill over a long period can be
        interrupted without leaving a half-computed day behind.
        """
        for day in days:
            await self.rollup_day(day, defaults)
        return len(days)

    async def get(self, day: date) -> AnalyticsDaily | None:
        """Return the stored summary for one day, if it has been rolled up."""
        result = await self.session.execute(
            select(AnalyticsDaily).where(AnalyticsDaily.day == day)
        )
        return result.scalar_one_or_none()
