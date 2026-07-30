"""A conversation thread between a user and the bot."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.message import Message
    from app.models.user import User

# Who is answering this conversation right now. Deliberately separate from
# ``status``, which tracks lifecycle (active / archived) and is depended on by
# the partial unique index uq_active_conversation_per_user. See
# alembic/versions/0004_conversation_handoff.py for why merging the two would
# split a customer's history in half.
MODE_BOT = "bot"
MODE_HUMAN = "human"

# Why this conversation needs a person. Separate from ``mode`` (who is
# answering) and from the handoff ``reason`` in the logs (what triggered it):
# the tag is the part an operator sorts and filters on, so it has to be a
# small, stable vocabulary rather than a free-form sentence.
#
# A customer who names a figure, asks for a discount, asks for the Sales
# Manager or asks to be called back is somebody about to spend money. That is
# worth putting at the top of the list; a routine question is not.
TAG_SALES_LEAD = "sales_lead"


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    # ``server_default`` as well as ``default``: get_or_create_active inserts
    # through pg_insert, which does not apply ORM-side defaults.
    mode: Mapped[str] = mapped_column(
        String(16), default=MODE_BOT, server_default=MODE_BOT, index=True
    )
    # Why a person is needed. Sticky on purpose: it is not cleared when the AI
    # resumes, because it records what this conversation turned out to be, not
    # who happens to be answering it right now. Reporting on how many leads
    # arrived last week needs the former.
    tag: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # Free text, not a foreign key: there is no operator account table yet, and
    # inventing one here would be a bigger change than the handoff itself.
    assigned_operator: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # When the CURRENT handoff started; cleared when the AI resumes.
    handoff_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="conversations")
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )
