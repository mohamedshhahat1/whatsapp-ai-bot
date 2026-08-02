"""A conversation thread between a user and the bot."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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

# Lifecycle of the session itself. ``active`` is the only value the partial
# unique index uq_active_conversation_per_user counts, which is what makes
# closing a session self-cleaning: it releases the customer's slot, and their
# next message opens a brand new conversation row with no flags to reset.
STATUS_ACTIVE = "active"
STATUS_CLOSED = "closed"

# Why this conversation needs a person. Separate from ``mode`` (who is
# answering) and from the handoff ``reason`` in the logs (what triggered it):
# the tag is the part an operator sorts and filters on, so it has to be a
# small, stable vocabulary rather than a free-form sentence.
#
# A customer who names a figure, asks for a discount, asks for the Sales
# Manager or asks to be called back is somebody about to spend money. That is
# worth putting at the top of the list; a routine question is not.
TAG_SALES_LEAD = "sales_lead"

# The session lifecycle as operators and the admin API see it. These are
# derived from (status, mode, last_activity_at) by ``session_state`` below and
# never stored. A stored copy would need something to keep it true, and the
# moment it disagreed with the columns it came from there would be no way to
# tell which of the two had gone wrong.
SESSION_ACTIVE_BOT = "ACTIVE_BOT"
SESSION_ACTIVE_HUMAN = "ACTIVE_HUMAN"
SESSION_WAITING_IDLE = "WAITING_IDLE"
SESSION_CLOSED = "CLOSED"


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default=STATUS_ACTIVE, index=True
    )
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
    # --- Session lifecycle (see 0007_conversation_session_lifecycle) --------
    # The idle timer counts from here. Written on every inbound message, every
    # reply in either direction, and every switch between bot and human, so
    # "last activity" means exactly that rather than "last customer message".
    #
    # server_default as well as default, for the same pg_insert reason as
    # ``mode``: a row created by get_or_create_active never passes through the
    # ORM default, and a null here would read as infinitely idle.
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # When the approved welcome copy actually reached the customer. Null means
    # this session has not greeted anyone yet, which is the only condition the
    # welcome is gated on -- one flag, checked and set in one place.
    welcome_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Claimed before the closing message is sent, never after. Non-null means
    # some worker already owns saying goodbye to this session, whether or not
    # the send has finished.
    closing_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When the session ended. Distinct from closing_sent_at because a session
    # can end without a goodbye: the feature can be off, or the 24-hour
    # service window can have closed before the sweep reached it.
    closed_at: Mapped[datetime | None] = mapped_column(
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

    @property
    def is_open(self) -> bool:
        """True while this session can still be added to."""
        return self.status == STATUS_ACTIVE

    def session_state(
        self, idle_after: timedelta, now: datetime | None = None
    ) -> str:
        """Which lifecycle state this conversation is in at ``now``.

        ``idle_after`` is passed in rather than read from settings so the
        model stays free of configuration, and so a caller listing fifty
        conversations resolves the timeout once instead of per row.

        A conversation held by a human is reported as ACTIVE_HUMAN even once
        it is quiet. That is not an oversight: WAITING_IDLE means "nobody is
        coming back to this and it is due to be closed", and a conversation
        an operator has taken is somebody's open work. The sweeper applies
        the same rule, so the state an operator reads and the state the
        closing logic acts on cannot drift apart.
        """
        if self.status != STATUS_ACTIVE:
            return SESSION_CLOSED
        if self.mode == MODE_HUMAN:
            return SESSION_ACTIVE_HUMAN
        moment = now or datetime.now(UTC)
        if moment - self.last_activity_at >= idle_after:
            return SESSION_WAITING_IDLE
        return SESSION_ACTIVE_BOT
