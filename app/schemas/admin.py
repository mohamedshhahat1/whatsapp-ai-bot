"""Admin/statistics schemas."""

from pydantic import BaseModel


class StatsRead(BaseModel):
    total_users: int
    total_conversations: int
    total_messages: int
    messages_last_24h: int
    total_tokens_used: int
