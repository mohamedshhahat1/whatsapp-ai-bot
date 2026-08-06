"""A conversation thread between a user and the bot."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.channels.constants import WHATSAPP
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
# derived from (status, mode, last_activity_at, closing_sent_at) by
# ``derive_session_state`` below and never stored. A stored copy would need
# something to keep it true, and the moment it disagreed with the columns it
# came from there would be no way to tell which of the two had gone wrong.
SESSION_ACTIVE_BOT = "ACTIVE_BOT"
SESSION_ACTIVE_HUMAN = "ACTIVE_HUMAN"
SESSION_WAITING_IDLE = "WAITING_IDLE"
SESSION_CLOSING = "CLOSING"
SESSION_CLOSED = "CLOSED"


def derive_session_state(
    *,
    status: str,
    mode: str,
    last_activity_at: datetime | None,
    closing_sent_at: datetime | None,
    idle_after: timedelta,
    now: datetime | None = None,
) -> str:
    """Which lifecycle state these column values describe.

    A free function over primitives rather than a method, because two callers
    need it and only one of them has an ORM object: the admin API serialises
    ``ConversationRead`` from plain fields. Reimplementing the rule there --
    the obvious alternative -- would put two copies of it in the codebase, and
    the drift between them would be silent in the worst way: the sweeper would
    close a session the dashboard was still calling active, and nothing would
    indicate which of the two was wrong.

    ``idle_after`` is passed in rather than read from settings so this stays
    free of configuration, and so a caller rendering fifty conversations
    resolves the timeout once instead of per row.

    Order matters. Each check below claims a state the later ones would
    otherwise also match:

    * CLOSED wins outright -- the session is over.
    * CLOSING is the window between a worker claiming the goodbye and the
      close committing. Short, but it is the only honest answer while it
      lasts, and it used to render as ACTIVE_BOT: an operator could open a
      conversation that was already being said goodbye to and reply into it.
    * ACTIVE_HUMAN outranks WAITING_IDLE even when the conversation is quiet.
      That is not an oversight. WAITING_IDLE means "nobody is coming back to
      this and it is due to be closed", and a conversation an operator has
      taken is somebody's open work. ``claim_idle_sessions`` applies exactly
      the same rule -- it only considers ``mode = bot`` -- so the state an
      operator reads and the state the closing logic acts on cannot disagree.
    """
    if status != STATUS_ACTIVE:
        return SESSION_CLOSED
    if closing_sent_at is not None:
        return SESSION_CLOSING
    if mode == MODE_HUMAN:
        return SESSION_ACTIVE_HUMAN
    if last_activity_at is None:  # pragma: no cover - column is NOT NULL
        return SESSION_ACTIVE_BOT
    moment = now or datetime.now(UTC)
    if moment - last_activity_at >= idle_after:
        return SESSION_WAITING_IDLE
    return SESSION_ACTIVE_BOT


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Which app the customer is writing from. Denormalised from users.channel
    # rather than joined: every dashboard list, analytics rollup and operator
    # filter wants it on the row, and a user's channel cannot change, so this
    # copy has nothing to go stale against.
    #
    # server_default as well as default, for the same pg_insert reason as
    # ``mode``: get_or_create_active never passes through the ORM default.
    channel: Mapped[str] = mapped_column(
        String(24), default=WHATSAPP, server_default=WHATSAPP, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default=STATUS_ACTIVE, index=True)
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
    # The operator's display label, as free text. This predates operator
    # accounts, every handed-off row carries one, and ConversationRead
    # serialises it to both clients -- so it stays. ``assigned_operator_id``
    # below is the authoritative reference now; this is kept in step with it
    # for backward compatibility rather than removed.
    assigned_operator: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Who currently owns this conversation. Nullable because the bot owns most
    # of them, and ON DELETE SET NULL because removing an operator account
    # must not delete the customer conversations they handled.
    assigned_operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("operators.id", ondelete="SET NULL"), nullable=True, index=True
    )
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

    def session_state(self, idle_after: timedelta, now: datetime | None = None) -> str:
        """Which lifecycle state this conversation is in at ``now``.

        Thin wrapper over :func:`derive_session_state`, which is shared with
        the admin API serializer so both answer this question identically.
        """
        return derive_session_state(
            status=self.status,
            mode=self.mode,
            last_activity_at=self.last_activity_at,
            closing_sent_at=self.closing_sent_at,
            idle_after=idle_after,
            now=now,
        )
