"""Managing the model pricing history."""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, NotFoundError
from app.models.model_pricing import ModelPricing
from app.repositories.model_pricing import ModelPricingRepository


class DuplicatePricingError(AppError):
    """Raised when a price already exists for that model and instant."""


class PricingService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._pricing = ModelPricingRepository(session)

    async def list(self) -> list[ModelPricing]:
        return await self._pricing.list()

    async def add(
        self,
        model: str,
        input_price_per_1m: float,
        output_price_per_1m: float,
        effective_from: datetime | None = None,
        note: str | None = None,
    ) -> ModelPricing:
        """Record a new price period for a model.

        Prices are converted through str() rather than passed as floats:
        Decimal(0.4) is 0.4000000000000000222..., while Decimal("0.4") is
        exactly 0.4.
        """
        try:
            pricing = await self._pricing.add(
                model=model,
                input_price_per_1m=Decimal(str(input_price_per_1m)),
                output_price_per_1m=Decimal(str(output_price_per_1m)),
                effective_from=effective_from or datetime.now(UTC),
                note=note,
            )
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise DuplicatePricingError(
                f"A price for {model} already exists at that effective date."
            ) from exc
        return pricing

    async def delete(self, pricing_id: int) -> None:
        """Remove a price period.

        Deleting rewrites history for any call that fell inside the deleted
        period, so this is for correcting mistakes, not for retiring a price.
        To retire one, add a newer row instead.
        """
        pricing = await self._pricing.get(pricing_id)
        if pricing is None:
            raise NotFoundError(f"Pricing row {pricing_id} not found")
        await self._pricing.delete(pricing)
        await self._session.commit()
