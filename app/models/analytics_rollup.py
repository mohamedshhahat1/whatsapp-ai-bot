"""Pre-aggregated daily analytics.

One row per UTC calendar day holding the figures
``AnalyticsRepository.daily_usage`` computes on the fly. The live query scans
ai_logs and messages across the whole requested window on every dashboard
load, and ai_logs takes one row per OpenAI call -- it is the fastest growing
table in the system, so that scan is the first thing here that stops being
viable.

Nothing reads this table yet. It is written by the nightly job while the
dashboard continues to answer from the live query, so that a bug in the
rollup cannot make the dashboard wrong before the two have been observed to
agree. Cutting the dashboard over is a separate change.

Every metric is one the application already reports. ``daily_usage`` supplies
requests, the three token sums, latency, cost and the message count;
``errors`` is ``usage_totals``' definition -- ``count()`` over the nullable
error column counts failures only -- applied per day.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Integer,
    Numeric,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AnalyticsDaily(Base):
    """One summary row per completed UTC day."""

    __tablename__ = "analytics_daily"

    # The day is the primary key, and that is what makes the rollup
    # idempotent: re-running a night collides and updates in place instead of
    # inserting a second row. The same index serves the range scans a reader
    # would issue (WHERE day >= ... ORDER BY day), so no secondary index is
    # needed -- adding one would only cost write time on a table whose whole
    # purpose is to be cheap to maintain.
    day: Mapped[date] = mapped_column(Date, primary_key=True)

    requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # BigInteger for the token sums. A busy day can run to tens of millions of
    # tokens, and while that still fits in an int4, the ceiling is close enough
    # that a backfill over a long period could reach it. There is no reason to
    # find out where the edge is.
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    total_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # Deliberately a SUM rather than the average daily_usage returns.
    #
    # An average is correct for the single day it describes and quietly wrong
    # the moment anyone averages several of them: a day with three requests
    # would count for as much as a day with thirty thousand. Keeping the sum
    # next to the request count means any window can be averaged exactly.
    # avg_latency_ms below gives readers the familiar figure.
    latency_ms_sum: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # Numeric, not float, and split the way usage_totals splits it rather than
    # pre-added the way daily_usage does. Nothing is lost -- cost_usd adds them
    # back -- and the breakdown stays recoverable.
    input_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=0
    )
    output_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=0
    )

    messages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # When this row was last recomputed. A rollup that reruns a night will move
    # this forward even when every figure is unchanged, which is what makes a
    # stalled scheduler visible: the newest updated_at stops advancing while
    # the days keep coming.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # These are cheap insurance against a future aggregation bug writing
        # nonsense rather than against bad input: every value here is produced
        # by a COUNT or a SUM over non-negative columns, so a violation means
        # the query is wrong, and failing the write is better than serving a
        # negative token count on a cost dashboard.
        CheckConstraint(
            "requests >= 0",
            name="ck_analytics_daily_requests_non_negative",
        ),
        # errors counts a subset of the same rows requests counts, so it can
        # never exceed it. If it ever does, the two are being computed over
        # different row sets.
        CheckConstraint(
            "errors >= 0 AND errors <= requests",
            name="ck_analytics_daily_errors_within_requests",
        ),
        CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND total_tokens >= 0",
            name="ck_analytics_daily_tokens_non_negative",
        ),
        CheckConstraint(
            "latency_ms_sum >= 0",
            name="ck_analytics_daily_latency_non_negative",
        ),
        CheckConstraint(
            "input_cost_usd >= 0 AND output_cost_usd >= 0",
            name="ck_analytics_daily_cost_non_negative",
        ),
        CheckConstraint(
            "messages >= 0",
            name="ck_analytics_daily_messages_non_negative",
        ),
    )

    @property
    def avg_latency_ms(self) -> float:
        """Mean latency for the day, or 0.0 when nothing was requested."""
        if not self.requests:
            return 0.0
        return self.latency_ms_sum / self.requests

    @property
    def cost_usd(self) -> Decimal:
        """Total spend for the day, matching daily_usage's single figure."""
        return self.input_cost_usd + self.output_cost_usd
