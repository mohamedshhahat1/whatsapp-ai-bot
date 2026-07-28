"""Usage and cost analytics.

Costs are derived, never stored: the OpenAI API returns token counts but no
price. Rates come from the model_pricing table, matched to the moment each
call was made, so changing the model or a price leaves historical figures
untouched. The values in Settings are only a fallback for calls that no
pricing row covers.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.repositories.analytics import AnalyticsRepository, PriceDefaults
from app.repositories.conversation import ConversationRepository
from app.repositories.message import MessageRepository
from app.repositories.user import UserRepository
from app.schemas.analytics import (
    AnalyticsOverview,
    CostBreakdown,
    CustomerActivityRead,
    DailyUsageRead,
    MessageHitRead,
    TopQuestionRead,
)
from app.schemas.pricing import ModelCostRead


class AnalyticsService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._analytics = AnalyticsRepository(session)
        self._users = UserRepository(session)
        self._conversations = ConversationRepository(session)
        self._messages = MessageRepository(session)
        self._settings = settings

    @property
    def _defaults(self) -> PriceDefaults:
        """Fallback prices for calls with no matching model_pricing row."""
        return PriceDefaults(
            input_price=Decimal(str(self._settings.openai_input_price_per_1m)),
            output_price=Decimal(str(self._settings.openai_output_price_per_1m)),
        )

    async def overview(self, days: int = 30) -> AnalyticsOverview:
        since = datetime.now(UTC) - timedelta(days=days)
        totals = await self._analytics.usage_totals(since, self._defaults)
        input_cost = round(totals.input_cost_usd, 6)
        output_cost = round(totals.output_cost_usd, 6)
        total_cost = round(input_cost + output_cost, 6)
        conversations = await self._conversations.count()

        return AnalyticsOverview(
            period_days=days,
            since=since,
            total_users=await self._users.count(),
            total_conversations=conversations,
            total_messages=await self._messages.count(),
            ai_requests=totals.requests,
            ai_errors=totals.errors,
            error_rate=(
                round(totals.errors / totals.requests, 4) if totals.requests else 0.0
            ),
            avg_latency_ms=round(totals.avg_latency_ms, 1),
            p95_latency_ms=round(totals.p95_latency_ms, 1),
            cost=CostBreakdown(
                prompt_tokens=totals.prompt_tokens,
                completion_tokens=totals.completion_tokens,
                total_tokens=totals.total_tokens,
                input_cost_usd=input_cost,
                output_cost_usd=output_cost,
                total_cost_usd=total_cost,
            ),
            cost_per_conversation_usd=(
                round(total_cost / conversations, 6) if conversations else 0.0
            ),
            projected_monthly_cost_usd=(
                round(total_cost / days * 30, 4) if days else 0.0
            ),
        )

    async def daily(self, days: int = 30) -> list[DailyUsageRead]:
        since = datetime.now(UTC) - timedelta(days=days)
        return [
            DailyUsageRead(
                day=row.day,
                requests=row.requests,
                messages=row.messages,
                prompt_tokens=row.prompt_tokens,
                completion_tokens=row.completion_tokens,
                total_tokens=row.total_tokens,
                avg_latency_ms=round(row.avg_latency_ms, 1),
                cost_usd=round(row.cost_usd, 6),
            )
            for row in await self._analytics.daily_usage(since, self._defaults)
        ]

    async def cost_by_model(self, days: int = 30) -> list[ModelCostRead]:
        since = datetime.now(UTC) - timedelta(days=days)
        return [
            ModelCostRead(
                model=row.model,
                requests=row.requests,
                prompt_tokens=row.prompt_tokens,
                completion_tokens=row.completion_tokens,
                total_tokens=row.total_tokens,
                cost_usd=round(row.cost_usd, 6),
            )
            for row in await self._analytics.cost_by_model(since, self._defaults)
        ]

    async def top_questions(
        self, days: int = 30, limit: int = 10
    ) -> list[TopQuestionRead]:
        since = datetime.now(UTC) - timedelta(days=days)
        return [
            TopQuestionRead(
                question=row.question, count=row.count, last_asked=row.last_asked
            )
            for row in await self._analytics.top_questions(since, limit=limit)
        ]

    async def customers(
        self, offset: int = 0, limit: int = 50
    ) -> list[CustomerActivityRead]:
        return [
            CustomerActivityRead(
                user_id=row.user_id,
                wa_id=row.wa_id,
                name=row.name,
                conversations=row.conversations,
                messages=row.messages,
                last_active=row.last_active,
            )
            for row in await self._analytics.customer_activity(
                offset=offset, limit=limit
            )
        ]

    async def search_messages(
        self, query: str, limit: int = 50
    ) -> list[MessageHitRead]:
        return [
            MessageHitRead(
                message_id=row.message_id,
                conversation_id=row.conversation_id,
                user_id=row.user_id,
                wa_id=row.wa_id,
                name=row.name,
                direction=row.direction,
                content=row.content,
                created_at=row.created_at,
            )
            for row in await self._analytics.search_messages(query, limit=limit)
        ]
