"""Usage and cost analytics.

Costs are derived, never stored: the OpenAI API does not return a price, so
spend is computed from logged token counts multiplied by the configured
per-1M rates. If prices change, update the settings and every historical
figure is recalculated at the new rate -- see docs/DASHBOARD.md for the
caveat this implies.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.repositories.analytics import AnalyticsRepository
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

TOKENS_PER_MILLION = 1_000_000


class AnalyticsService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._analytics = AnalyticsRepository(session)
        self._users = UserRepository(session)
        self._conversations = ConversationRepository(session)
        self._messages = MessageRepository(session)
        self._settings = settings

    def _costs(self, prompt_tokens: int, completion_tokens: int) -> tuple[float, float]:
        input_cost = (
            prompt_tokens / TOKENS_PER_MILLION * self._settings.openai_input_price_per_1m
        )
        output_cost = (
            completion_tokens
            / TOKENS_PER_MILLION
            * self._settings.openai_output_price_per_1m
        )
        return round(input_cost, 6), round(output_cost, 6)

    async def overview(self, days: int = 30) -> AnalyticsOverview:
        since = datetime.now(UTC) - timedelta(days=days)
        totals = await self._analytics.usage_totals(since)
        input_cost, output_cost = self._costs(
            totals.prompt_tokens, totals.completion_tokens
        )
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
        rows = await self._analytics.daily_usage(since)
        result: list[DailyUsageRead] = []
        for row in rows:
            input_cost, output_cost = self._costs(
                row.prompt_tokens, row.completion_tokens
            )
            result.append(
                DailyUsageRead(
                    day=row.day,
                    requests=row.requests,
                    messages=row.messages,
                    prompt_tokens=row.prompt_tokens,
                    completion_tokens=row.completion_tokens,
                    total_tokens=row.total_tokens,
                    avg_latency_ms=round(row.avg_latency_ms, 1),
                    cost_usd=round(input_cost + output_cost, 6),
                )
            )
        return result

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

    async def search_messages(self, query: str, limit: int = 50) -> list[MessageHitRead]:
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
