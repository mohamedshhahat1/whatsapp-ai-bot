"""Aggregate read queries powering the cost and usage dashboard.

Everything here is expressed as SQL aggregates rather than loading rows into
Python. The ai_logs table gets one row per OpenAI call, so it is the fastest
growing table in the system; summing it in application code would stop being
viable within a few hundred thousand rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select

from app.models.ai_log import AILog
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.repositories.base import BaseRepository


@dataclass(frozen=True)
class UsageTotals:
    requests: int
    errors: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    avg_latency_ms: float
    p95_latency_ms: float


@dataclass(frozen=True)
class DailyUsage:
    day: date
    requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    avg_latency_ms: float
    messages: int


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


class AnalyticsRepository(BaseRepository):
    """Read-only aggregates over ai_logs, messages, conversations and users."""

    async def usage_totals(self, since: datetime) -> UsageTotals:
        """Request counts, token sums and latency percentiles since a cutoff."""
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
            ).where(AILog.created_at >= since)
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
        )

    async def daily_usage(self, since: datetime) -> list[DailyUsage]:
        """Per-day AI usage joined with per-day message volume.

        Days are unioned across both tables: a day can have messages without
        AI calls (bot disabled, manual replies only) or AI calls without
        inbound messages (retries, background jobs).
        """
        log_day = func.date_trunc("day", AILog.created_at).label("day")
        log_result = await self.session.execute(
            select(
                log_day,
                func.count(AILog.id),
                func.coalesce(func.sum(AILog.prompt_tokens), 0),
                func.coalesce(func.sum(AILog.completion_tokens), 0),
                func.coalesce(func.sum(AILog.total_tokens), 0),
                func.coalesce(func.avg(AILog.latency_ms), 0.0),
            )
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
                    messages=messages_by_day.get(day, 0),
                )
            )
        return usage

    async def top_questions(self, since: datetime, limit: int = 10) -> list[TopQuestion]:
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
            TopQuestion(
                question=str(row[0]), count=int(row[1]), last_asked=row[2]
            )
            for row in result.all()
        ]

    async def customer_activity(
        self, offset: int = 0, limit: int = 50
    ) -> list[CustomerActivity]:
        """Per-customer conversation and message counts.

        Correlated subqueries rather than joins: joining users to both
        conversations and messages would multiply the rows and inflate every
        count (the classic join fan-out).
        """
        conversation_count = (
            select(func.count(Conversation.id))
            .where(Conversation.user_id == User.id)
            .scalar_subquery()
        )
        message_count = (
            select(func.count(Message.id))
            .select_from(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(Conversation.user_id == User.id)
            .scalar_subquery()
        )
        last_active = (
            select(func.max(Message.created_at))
            .select_from(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(Conversation.user_id == User.id)
            .scalar_subquery()
        )
        result = await self.session.execute(
            select(
                User.id,
                User.wa_id,
                User.name,
                conversation_count.label("conversations"),
                message_count.label("messages"),
                last_active.label("last_active"),
            )
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
        """Case-insensitive substring search across message bodies."""
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
            .where(Message.content.ilike("%" + query + "%"))
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
