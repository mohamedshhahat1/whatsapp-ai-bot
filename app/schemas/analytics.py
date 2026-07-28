"""Cost tracking and usage analytics schemas."""

from datetime import date, datetime

from pydantic import BaseModel


class CostBreakdown(BaseModel):
    """Token spend converted to USD at the configured per-1M prices."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    input_cost_usd: float
    output_cost_usd: float
    total_cost_usd: float


class AnalyticsOverview(BaseModel):
    period_days: int
    since: datetime

    total_users: int
    total_conversations: int
    total_messages: int

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
