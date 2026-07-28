"""Model pricing data access."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from app.models.model_pricing import ModelPricing
from app.repositories.base import BaseRepository


class ModelPricingRepository(BaseRepository):
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

    async def effective_at(self, model: str, at: datetime) -> ModelPricing | None:
        """The price in force for a model at a given instant."""
        return await self.session.scalar(
            select(ModelPricing)
            .where(
                ModelPricing.model == model,
                ModelPricing.effective_from <= at,
            )
            .order_by(ModelPricing.effective_from.desc())
            .limit(1)
        )

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
