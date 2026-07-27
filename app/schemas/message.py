"""Message response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    wa_message_id: str | None
    direction: str
    type: str
    content: str | None
    media_id: str | None
    status: str | None
    created_at: datetime
