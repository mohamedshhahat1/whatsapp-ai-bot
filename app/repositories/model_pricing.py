"""Model pricing data access."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from app.models.model_pricing import ModelPricing
from app.repositories.base import BaseRepository


class ModelPricingRepository(BaseRepository):
    """CRUD for price periods.

    Resolving which price applied to a given call is deliberately not here:
    doing it per row would be an N+1 against ai_logs, so the analytics queries
    resolve it in SQL with a LATERAL join (app/repositories/analytics.py).
    """

    async def list(self) -> list[ModelPricing]:
        """All price rows, newest period first."""
        result = await self.session.scalars(
            select(ModelPricing).order_by(
                ModelPricing.model, ModelPricing.effective_from.desc()
            )
        )
        return list(result)

    async def get(self, pricing_id: int) -> ModelPricing | None:
        return await self.session.get(ModelPricing, pricing_id)

    async def add(
        self,
        model: str,
        input_price_per_1m: Decimal,
        output_price_per_1m: Decimal,
        effective_from: datetime,
        note: str | None = None,
    ) -> ModelPricing:
        pricing = ModelPricing(
            model=model,
            input_price_per_1m=input_price_per_1m,
            output_price_per_1m=output_price_per_1m,
            effective_from=effective_from,
            note=note,
        )
        self.session.add(pricing)
        await self.session.flush()
        return pricing

    async def delete(self, pricing: ModelPricing) -> None:
        await self.session.delete(pricing)
