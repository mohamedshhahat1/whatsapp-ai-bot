"""Aggregate read queries powering the cost and usage dashboard.

Everything here is expressed as SQL aggregates rather than loading rows into
Python. The ai_logs table gets one row per OpenAI call, so it is the fastest
growing table in the system; summing it in application code would stop being
viable within a few hundred thousand rows.

Cost is resolved per row against the model_pricing table, using the price that
was in force when the call was made rather than today's price.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Numeric, cast, func, literal, select, true

from app.models.ai_log import AILog
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.model_pricing import ModelPricing
from app.models.user import User
from app.repositories.base import BaseRepository

# Prices are quoted per million tokens.
MILLION = literal(Decimal(1_000_000))


@dataclass(frozen=True)
class PriceDefaults:
    """Fallback used when no model_pricing row covers a call.

    Keeps the dashboard honest on a fresh database, or if someone logs a call
    with a model that has never been priced.
    """

    input_price: Decimal
    output_price: Decimal


@dataclass(frozen=True)
class ActivityTotals:
    """Lifetime and in-window volume counts.

    The two are kept apart deliberately. Presenting an all-time customer count
    next to a 30-day spend figure invites the reader to divide one by the
    other, which is how cost-per-conversation ended up wrong.
    """

    total_users: int
    total_conversations: int
    total_messages: int
    new_users: int
    new_conversations: int
    active_conversations: int
    messages_in_period: int


@dataclass(frozen=True)
class UsageTotals:
    requests: int
    errors: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    avg_latency_ms: float
    p95_latency_ms: float
    input_cost_usd: float
    output_cost_usd: float


@dataclass(frozen=True)
class DailyUsage:
    day: date
    requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    avg_latency_ms: float
    cost_usd: float
    messages: int


@dataclass(frozen=True)
class ModelCost:
    model: str
    requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float


@dataclass(frozen=True)
class TopQuestion:
    question: str
    count: int
    last_asked: datetime


@dataclass(frozen=True)
class CustomerActivity:
    user_id: int
    wa_id: str
    name: str | None
    conversations: int
    messages: int
    last_active: datetime | None


@dataclass(frozen=True)
class MessageHit:
    message_id: int
    conversation_id: int
    user_id: int
    wa_id: str
    name: str | None
    direction: str
    content: str
    created_at: datetime


def _pricing_lateral() -> Any:
    """The price row in force for each ai_logs row.

    A correlated LATERAL subquery with LIMIT 1 is the standard way to express
    an as-of join in Postgres. Because it returns at most one row and is
    joined with LEFT JOIN, it cannot change the number of ai_logs rows, so
    counts and averages in the same query stay correct.
    """
    return (
        select(
            ModelPricing.input_price_per_1m.label("input_price"),
            ModelPricing.output_price_per_1m.label("output_price"),
        )
        .where(
            ModelPricing.model == AILog.model,
            ModelPricing.effective_from <= AILog.created_at,
        )
        .order_by(ModelPricing.effective_from.desc())
        .limit(1)
        .lateral("pricing")
    )


def _cost_parts(pricing: Any, defaults: PriceDefaults) -> tuple[Any, Any]:
    """Per-row input and output cost in USD, as Numeric expressions."""
    input_price = func.coalesce(pricing.c.input_price, literal(defaults.input_price))
    output_price = func.coalesce(pricing.c.output_price, literal(defaults.output_price))
    input_cost = cast(AILog.prompt_tokens, Numeric) / MILLION * input_price
    output_cost = cast(AILog.completion_tokens, Numeric) / MILLION * output_price
    return input_cost, output_cost


def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so % and _ are matched literally.

    Without this, searching for "50%" returns every message in the database
    and "a_b" matches "axb" -- the operator gets confidently wrong results
    with no indication anything went wrong.
    """
    for char in ("\\", "%", "_"):
        term = term.replace(char, "\\" + char)
    return term


class AnalyticsRepository(BaseRepository):
    """Read-only aggregates over ai_logs, messages, conversations and users."""

    async def activity_totals(self, since: datetime) -> ActivityTotals:
        """Lifetime and in-window volume counts in a single round trip.

        These were four separate awaited COUNT(*) queries issued one after
        another. Postgres can evaluate them as uncorrelated scalar subqueries
        in one statement, which costs one round trip instead of four and
        gives every figure the same snapshot of the data.
        """
        total_users = select(func.count(User.id)).scalar_subquery()
        total_conversations = select(func.count(Conversation.id)).scalar_subquery()
        total_messages = select(func.count(Message.id)).scalar_subquery()
        new_users = (
            select(func.count(User.id))
            .where(User.created_at >= since)
            .scalar_subquery()
        )
        new_conversations = (
            select(func.count(Conversation.id))
            .where(Conversation.created_at >= since)
            .scalar_subquery()
        )
        active_conversations = (
            select(func.count(func.distinct(Message.conversation_id)))
            .where(Message.created_at >= since)
            .scalar_subquery()
        )
        messages_in_period = (
            select(func.count(Message.id))
            .where(Message.created_at >= since)
            .scalar_subquery()
        )
        row = (
            await self.session.execute(
                select(
                    total_users,
                    total_conversations,
                    total_messages,
                    new_users,
                    new_conversations,
                    active_conversations,
                    messages_in_period,
                )
            )
        ).one()
        return ActivityTotals(
            total_users=int(row[0]),
            total_conversations=int(row[1]),
            total_messages=int(row[2]),
            new_users=int(row[3]),
            new_conversations=int(row[4]),
            active_conversations=int(row[5]),
            messages_in_period=int(row[6]),
        )

    async def usage_totals(
        self, since: datetime, defaults: PriceDefaults
    ) -> UsageTotals:
        """Request counts, token sums, latency percentiles and spend."""
        pricing = _pricing_lateral()
        input_cost, output_cost = _cost_parts(pricing, defaults)
        result = await self.session.execute(
            select(
                func.count(AILog.id),
                # count() of a nullable column counts non-NULLs only, which is
                # exactly the number of failed calls.
                func.count(AILog.error),
                func.coalesce(func.sum(AILog.prompt_tokens), 0),
                func.coalesce(func.sum(AILog.completion_tokens), 0),
                func.coalesce(func.sum(AILog.total_tokens), 0),
                func.coalesce(func.avg(AILog.latency_ms), 0.0),
                func.coalesce(
                    func.percentile_cont(0.95).within_group(AILog.latency_ms.asc()),
                    0.0,
                ),
                func.coalesce(func.sum(input_cost), 0),
                func.coalesce(func.sum(output_cost), 0),
            )
            .select_from(AILog)
            .outerjoin(pricing, true())
            .where(AILog.created_at >= since)
        )
        row = result.one()
        return UsageTotals(
            requests=int(row[0]),
            errors=int(row[1]),
            prompt_tokens=int(row[2]),
            completion_tokens=int(row[3]),
            total_tokens=int(row[4]),
            avg_latency_ms=float(row[5]),
            p95_latency_ms=float(row[6]),
            input_cost_usd=float(row[7]),
            output_cost_usd=float(row[8]),
        )

    async def daily_usage(
        self, since: datetime, defaults: PriceDefaults
    ) -> list[DailyUsage]:
        """Per-day AI usage joined with per-day message volume.

        Days are unioned across both tables: a day can have messages without
        AI calls (bot disabled, manual replies only) or AI calls without
        inbound messages (retries, background jobs).
        """
        pricing = _pricing_lateral()
        input_cost, output_cost = _cost_parts(pricing, defaults)
        log_day = func.date_trunc("day", AILog.created_at).label("day")
        log_result = await self.session.execute(
            select(
                log_day,
                func.count(AILog.id),
                func.coalesce(func.sum(AILog.prompt_tokens), 0),
                func.coalesce(func.sum(AILog.completion_tokens), 0),
                func.coalesce(func.sum(AILog.total_tokens), 0),
                func.coalesce(func.avg(AILog.latency_ms), 0.0),
                func.coalesce(func.sum(input_cost + output_cost), 0),
            )
            .select_from(AILog)
            .outerjoin(pricing, true())
            .where(AILog.created_at >= since)
            .group_by(log_day)
        )
        logs_by_day = {row[0].date(): row for row in log_result.all()}

        message_day = func.date_trunc("day", Message.created_at).label("day")
        message_result = await self.session.execute(
            select(message_day, func.count(Message.id))
            .where(Message.created_at >= since)
            .group_by(message_day)
        )
        messages_by_day = {row[0].date(): int(row[1]) for row in message_result.all()}

        days = sorted(set(logs_by_day) | set(messages_by_day))
        usage: list[DailyUsage] = []
        for day in days:
            row = logs_by_day.get(day)
            usage.append(
                DailyUsage(
                    day=day,
                    requests=int(row[1]) if row else 0,
                    prompt_tokens=int(row[2]) if row else 0,
                    completion_tokens=int(row[3]) if row else 0,
                    total_tokens=int(row[4]) if row else 0,
                    avg_latency_ms=float(row[5]) if row else 0.0,
                    cost_usd=float(row[6]) if row else 0.0,
                    messages=messages_by_day.get(day, 0),
                )
            )
        return usage

    async def cost_by_model(
        self, since: datetime, defaults: PriceDefaults
    ) -> list[ModelCost]:
        """Spend split per model, each costed at its own historical rates."""
        pricing = _pricing_lateral()
        input_cost, output_cost = _cost_parts(pricing, defaults)
        total_cost = func.coalesce(func.sum(input_cost + output_cost), 0).label("cost")
        result = await self.session.execute(
            select(
                AILog.model,
                func.count(AILog.id),
                func.coalesce(func.sum(AILog.prompt_tokens), 0),
                func.coalesce(func.sum(AILog.completion_tokens), 0),
                func.coalesce(func.sum(AILog.total_tokens), 0),
                total_cost,
            )
            .select_from(AILog)
            .outerjoin(pricing, true())
            .where(AILog.created_at >= since)
            .group_by(AILog.model)
            .order_by(total_cost.desc())
        )
        return [
            ModelCost(
                model=str(row[0]),
                requests=int(row[1]),
                prompt_tokens=int(row[2]),
                completion_tokens=int(row[3]),
                total_tokens=int(row[4]),
                cost_usd=float(row[5]),
            )
            for row in result.all()
        ]

    async def top_questions(
        self, since: datetime, limit: int = 10
    ) -> list[TopQuestion]:
        """Most frequently asked inbound messages, grouped by normalised text.

        This groups on exact (lowercased, whitespace-collapsed) text, so it
        surfaces repeated phrasings rather than semantic clusters. Very short
        messages are excluded because greetings would otherwise dominate.
        """
        normalised = func.lower(
            func.regexp_replace(func.trim(Message.content), r"\s+", " ", "g")
        ).label("question")
        result = await self.session.execute(
            select(
                normalised,
                func.count(Message.id).label("count"),
                func.max(Message.created_at).label("last_asked"),
            )
            .where(
                Message.direction == "inbound",
                Message.content.is_not(None),
                Message.created_at >= since,
                func.length(func.trim(Message.content)) >= 8,
            )
            .group_by(normalised)
            .order_by(func.count(Message.id).desc())
            .limit(limit)
        )
        return [
            TopQuestion(question=str(row[0]), count=int(row[1]), last_asked=row[2])
            for row in result.all()
        ]

    async def customer_activity(
        self, offset: int = 0, limit: int = 50
    ) -> list[CustomerActivity]:
        """Per-customer conversation and message counts.

        One grouped aggregate rather than three correlated subqueries per row.
        The correlated form could not be short-circuited by the LIMIT anyway:
        ordering by last activity forces every user's subqueries to be
        evaluated before the first page can be chosen.

        count(DISTINCT conversation_id) is what keeps the join fan-out from
        inflating the conversation count -- the messages join multiplies each
        conversation row by its message count.
        """
        activity = (
            select(
                Conversation.user_id.label("user_id"),
                func.count(func.distinct(Conversation.id)).label("conversations"),
                func.count(Message.id).label("messages"),
                func.max(Message.created_at).label("last_active"),
            )
            .select_from(Conversation)
            .outerjoin(Message, Message.conversation_id == Conversation.id)
            .group_by(Conversation.user_id)
            .subquery()
        )
        last_active = activity.c.last_active
        result = await self.session.execute(
            select(
                User.id,
                User.wa_id,
                User.name,
                func.coalesce(activity.c.conversations, 0),
                func.coalesce(activity.c.messages, 0),
                last_active,
            )
            .outerjoin(activity, activity.c.user_id == User.id)
            .order_by(func.coalesce(last_active, User.created_at).desc())
            .offset(offset)
            .limit(limit)
        )
        return [
            CustomerActivity(
                user_id=int(row[0]),
                wa_id=str(row[1]),
                name=row[2],
                conversations=int(row[3]),
                messages=int(row[4]),
                last_active=row[5],
            )
            for row in result.all()
        ]

    async def search_messages(self, query: str, limit: int = 50) -> list[MessageHit]:
        """Case-insensitive substring search across message bodies.

        The leading wildcard rules out a B-tree index, so this is served by
        ix_messages_content_trgm (pg_trgm GIN), added in migration
        0003_search_and_concurrency.
        """
        pattern = "%" + _escape_like(query) + "%"
        result = await self.session.execute(
            select(
                Message.id,
                Message.conversation_id,
                User.id,
                User.wa_id,
                User.name,
                Message.direction,
                Message.content,
                Message.created_at,
            )
            .join(Conversation, Message.conversation_id == Conversation.id)
            .join(User, Conversation.user_id == User.id)
            .where(Message.content.ilike(pattern, escape="\\"))
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        return [
            MessageHit(
                message_id=int(row[0]),
                conversation_id=int(row[1]),
                user_id=int(row[2]),
                wa_id=str(row[3]),
                name=row[4],
                direction=str(row[5]),
                content=str(row[6] or ""),
                created_at=row[7],
            )
            for row in result.all()
        ]
