"""Token prices per model, valid from a point in time.

The OpenAI API never returns a price, only token counts, so spend has to be
derived. Deriving it from a single global setting means changing the model or
a price silently rewrites history: last quarter's report would be recomputed
at today's rate.

This table makes pricing temporal. Each row says "from this instant, this
model costs this much", and every AI call is costed with the row that was in
force when the call was made. Rows are never edited, only superseded by a
newer effective_from -- that is what keeps old numbers stable.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Index, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ModelPricing(Base):
    __tablename__ = "model_pricing"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Matches ai_logs.model exactly, e.g. "gpt-4.1-mini".
    model: Mapped[str] = mapped_column(String(64), index=True)

    # USD per 1,000,000 tokens. Numeric, not Float: these are money, and
    # binary floats cannot represent values like 0.40 exactly.
    input_price_per_1m: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    output_price_per_1m: Mapped[Decimal] = mapped_column(Numeric(12, 6))

    # The instant this price took effect. Calls before it use the previous row.
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    note: Mapped[str | None] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        # One price per model per instant; a repeated effective_from would
        # make the lookup ambiguous.
        UniqueConstraint("model", "effective_from", name="uq_model_pricing_period"),
        # Supports the as-of lookup. Ascending is fine: Postgres scans a
        # btree backwards just as fast, so no DESC index is needed.
        Index("ix_model_pricing_lookup", "model", "effective_from"),
    )
