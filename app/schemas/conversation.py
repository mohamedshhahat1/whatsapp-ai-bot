"""Conversation response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.message import MessageRead


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    # Lifecycle and ownership are separate axes: a conversation stays "active"
    # for the whole time a human operator owns it.
    status: str
    mode: str
    assigned_operator: str | None = None
    handoff_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationRead):
    messages: list[MessageRead]


class HandoffRequest(BaseModel):
    """Body for taking a conversation over. The operator name is optional.

    There are no operator accounts yet, so this is a label the dashboard sends
    to stop two people answering the same customer -- not an authenticated
    identity.
    """

    operator: str | None = Field(
        default=None,
        max_length=64,
        description="Name of the operator taking the conversation over.",
    )
