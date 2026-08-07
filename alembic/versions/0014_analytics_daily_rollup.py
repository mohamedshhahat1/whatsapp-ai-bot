"""Nightly analytics rollup table.

Stores one pre-aggregated row per UTC calendar day, so the cost dashboard
stops re-scanning ai_logs and messages for the whole window on every load.

The day is the primary key. That is not only the natural key -- it is what
makes the rollup job idempotent, because a re-run for the same night collides
and updates in place instead of inserting a duplicate. It also serves the
range scans a reader issues, so no secondary index is created.

Nothing reads the table yet, so this migration is additive and safe to apply
ahead of the code that writes it.

Revision ID: 0014_analytics_daily_rollup
Revises: 0013_external_id_not_null
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_analytics_daily_rollup"
down_revision: str | None = "0013_external_id_not_null"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analytics_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column(
            "requests", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("errors", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "prompt_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "completion_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "total_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "latency_ms_sum",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "input_cost_usd",
            sa.Numeric(18, 6),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "output_cost_usd",
            sa.Numeric(18, 6),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "messages", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("day", name="pk_analytics_daily"),
        # Every value is a COUNT or a SUM over non-negative columns, so these
        # cannot fire on valid input. They exist to stop a future aggregation
        # bug from persisting nonsense onto a cost dashboard.
        sa.CheckConstraint(
            "requests >= 0",
            name="ck_analytics_daily_requests_non_negative",
        ),
        sa.CheckConstraint(
            "errors >= 0 AND errors <= requests",
            name="ck_analytics_daily_errors_within_requests",
        ),
        sa.CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND total_tokens >= 0",
            name="ck_analytics_daily_tokens_non_negative",
        ),
        sa.CheckConstraint(
            "latency_ms_sum >= 0",
            name="ck_analytics_daily_latency_non_negative",
        ),
        sa.CheckConstraint(
            "input_cost_usd >= 0 AND output_cost_usd >= 0",
            name="ck_analytics_daily_cost_non_negative",
        ),
        sa.CheckConstraint(
            "messages >= 0",
            name="ck_analytics_daily_messages_non_negative",
        ),
    )


def downgrade() -> None:
    # The table holds only derived data: dropping it loses nothing that cannot
    # be recomputed from ai_logs and messages by re-running the rollup.
    op.drop_table("analytics_daily")
