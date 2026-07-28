"""Cost tracking and usage analytics schemas."""

from datetime import date, datetime

from pydantic import BaseModel


class CostBreakdown(BaseModel):
    """Token spend converted to USD at the historical per-1M prices."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    input_cost_usd: float
    output_cost_usd: float
    total_cost_usd: float


class AnalyticsOverview(BaseModel):
    """Headline KPIs for a reporting window.

    ``total_*`` fields are lifetime; ``new_*``, ``active_*`` and
    ``messages_in_period`` are scoped to ``period_days``. Every cost figure is
    scoped to the window, so window-scoped denominators are the only correct
    ones to divide them by.
    """

    period_days: int
    since: datetime

    total_users: int
    total_conversations: int
    total_messages: int

    new_users: int
    new_conversations: int
    active_conversations: int
    messages_in_period: int

    ai_requests: int
    ai_errors: int
    error_rate: float

    avg_latency_ms: float
    p95_latency_ms: float

    cost: CostBreakdown
    cost_per_conversation_usd: float
    projected_monthly_cost_usd: float


class DailyUsageRead(BaseModel):
    day: date
    requests: int
    messages: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    avg_latency_ms: float
    cost_usd: float


class TopQuestionRead(BaseModel):
    question: str
    count: int
    last_asked: datetime


class CustomerActivityRead(BaseModel):
    user_id: int
    wa_id: str
    name: str | None
    conversations: int
    messages: int
    last_active: datetime | None


class MessageHitRead(BaseModel):
    message_id: int
    conversation_id: int
    user_id: int
    wa_id: str
    name: str | None
    direction: str
    content: str
    created_at: datetime


class ManualReplyRequest(BaseModel):
    text: str


class ManualReplyResponse(BaseModel):
    message_id: int
    conversation_id: int
    wa_message_id: str | None
    sent_at: datetime
