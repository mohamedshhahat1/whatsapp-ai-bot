"""Temporal model pricing.

Revision ID: 0002_model_pricing
Revises: 0001_knowledge_base
Create Date: 2026-07-28

Seeds the currently configured model at the Unix epoch so that every existing
ai_logs row keeps exactly the cost it had before this migration. Without the
seed, historical calls would fall back to the settings defaults anyway, but an
explicit row makes the pricing history visible in the dashboard.
"""

from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from alembic import op

revision = "0002_model_pricing"
down_revision = "0001_knowledge_base"
branch_labels = None
depends_on = None

# Any call older than the first real price change is costed at the seeded
# rate, so the seed has to start at the beginning of time.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def upgrade() -> None:
    pricing = op.create_table(
        "model_pricing",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("input_price_per_1m", sa.Numeric(12, 6), nullable=False),
        sa.Column("output_price_per_1m", sa.Numeric(12, 6), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("model", "effective_from", name="uq_model_pricing_period"),
    )
    op.create_index("ix_model_pricing_model", "model_pricing", ["model"])
    op.create_index(
        "ix_model_pricing_lookup", "model_pricing", ["model", "effective_from"]
    )

    # Imported here rather than at module scope: Alembic loads every revision
    # file up front, and application settings should not be required for that.
    from app.config import get_settings

    settings = get_settings()
    op.bulk_insert(
        pricing,
        [
            {
                "model": settings.openai_model,
                "input_price_per_1m": Decimal(
                    str(settings.openai_input_price_per_1m)
                ),
                "output_price_per_1m": Decimal(
                    str(settings.openai_output_price_per_1m)
                ),
                "effective_from": EPOCH,
                "note": "Seeded from settings when pricing history was introduced",
            }
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_model_pricing_lookup", table_name="model_pricing")
    op.drop_index("ix_model_pricing_model", table_name="model_pricing")
    op.drop_table("model_pricing")
