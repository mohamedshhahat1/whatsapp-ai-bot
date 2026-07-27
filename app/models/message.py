"""A single inbound or outbound WhatsApp message."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.conversation import Conversation


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    wa_message_id: Mapped[str | None] = mapped_column(
        String(128), unique=True, index=True
    )
    direction: Mapped[str] = mapped_column(String(10))  # "inbound" | "outbound"
    type: Mapped[str] = mapped_column(String(20), default="text")
    content: Mapped[str | None] = mapped_column(Text)
    media_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str | None] = mapped_column(String(20))  # sent/delivered/read/failed
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
