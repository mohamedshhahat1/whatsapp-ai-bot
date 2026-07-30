"""Admin-facing views of the quota and spend subsystem."""

from pydantic import BaseModel, Field


class QuotaLimits(BaseModel):
    per_minute: int
    per_hour: int
    per_day: int


class QuotaStatsRead(BaseModel):
    """Today's cost position and the state of the protections.

    ``available`` is false when Redis could not be reached. The figures are
    then meaningless rather than zero, and the dashboard must say so instead
    of drawing a reassuring empty chart -- the guard is failing open at that
    moment, which is exactly when an operator needs to know.
    """

    available: bool
    error: str | None = None

    date: str | None = None
    spend_usd: float | None = None
    spend_limit_usd: float | None = None
    spend_used_fraction: float | None = None
    tokens: int | None = None
    token_limit: int | None = None

    ai_disabled: bool | None = None
    spend_guard_enabled: bool | None = None
    customer_rate_limit_enabled: bool | None = None
    blocked_customers: int | None = None
    limits: QuotaLimits | None = None


class AiToggleRequest(BaseModel):
    disabled: bool = Field(
        description=(
            "True stops the assistant answering anyone. Customer messages are "
            "still received, stored and shown to operators."
        )
    )


class AiToggleResponse(BaseModel):
    ai_disabled: bool


class UnblockResponse(BaseModel):
    wa_id: str
    was_blocked: bool
