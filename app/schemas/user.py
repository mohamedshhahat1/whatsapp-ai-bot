"""User response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    wa_id: str
    name: str | None
    created_at: datetime
