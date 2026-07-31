"""A single inbound or outbound WhatsApp message."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.conversation import Conversation

# Lifecycle of an outbound row. "pending" is written BEFORE the WhatsApp call
# and is what makes a retry safe: seeing one means a send may already have
# gone out, so the retry must not send again.
STATUS_PENDING = "pending"
STATUS_SENT = "sent"
# Reserved, then the process died before WhatsApp confirmed. The customer may
# or may not have received it; we deliberately do not guess.
STATUS_UNCONFIRMED = "unconfirmed"
STATUS_FAILED = "failed"


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    wa_message_id: Mapped[str | None] = mapped_column(
        String(128), unique=True, index=True
    )
    #: For an AI reply, the ``wa_message_id`` of the customer message it
    #: answers. Unique, so one inbound message can have at most one reply --
    #: enforced by the database rather than by whoever calls the service next.
    #:
    #: NULL for operator replies from the dashboard and for fixed copy sent
    #: without a model call. Postgres permits unlimited NULLs in a unique
    #: constraint, so those rows never contend with each other.
    reply_to_wa_message_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    direction: Mapped[str] = mapped_column(String(10))  # "inbound" | "outbound"
    type: Mapped[str] = mapped_column(String(20), default="text")
    content: Mapped[str | None] = mapped_column(Text)
    media_id: Mapped[str | None] = mapped_column(String(128))
    # pending/sent/unconfirmed/delivered/read/failed
    status: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
