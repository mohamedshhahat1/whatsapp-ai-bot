"""Audit log for every OpenAI call (usage, latency, errors)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AILog(Base):
    __tablename__ = "ai_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Its own key to tenants, and deliberately NOT folded into a composite key
    # with conversation_id below.
    #
    # conversation_id is ON DELETE SET NULL so that deleting a conversation
    # does not delete its cost record. A composite key would make Postgres
    # null BOTH columns on that delete, silently detaching the usage row from
    # the tenant that gets billed for it -- which is exactly the defect this
    # column exists to fix. So the tenant is kept independent of the
    # conversation's lifetime, and RESTRICT keeps it from vanishing.
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), index=True
    )
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), index=True
    )
    model: Mapped[str] = mapped_column(String(64))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
