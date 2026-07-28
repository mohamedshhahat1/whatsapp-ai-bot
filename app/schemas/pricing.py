"""Model pricing schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelPricingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    model: str
    input_price_per_1m: float
    output_price_per_1m: float
    effective_from: datetime
    note: str | None
    created_at: datetime


class ModelPricingCreate(BaseModel):
    model: str = Field(min_length=1, max_length=64)
    input_price_per_1m: float = Field(ge=0)
    output_price_per_1m: float = Field(ge=0)
    # Defaults to now: the common case is recording a price change as it
    # happens. Backdating is allowed for filling in history.
    effective_from: datetime | None = None
    note: str | None = Field(default=None, max_length=255)


class ModelCostRead(BaseModel):
    model: str
    requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
