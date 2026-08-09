"""A single inbound or outbound message on any channel."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Text,
    func,
)
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
    __table_args__ = (
        # The tenant travels with the parent reference rather than beside it.
        # A message can therefore only belong to a conversation in its own
        # tenant, and the database enforces that instead of trusting every
        # write path to remember it.
        #
        # CASCADE is carried over from 0000 unchanged. Widening the key
        # changes which rows are acceptable, not what a delete does.
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_messages_conversation",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # No separate key to tenants: the composite key above already makes a
    # tenant this row's conversation does not belong to unreferenceable, and
    # conversations in turn cannot outlive their tenant.
    tenant_id: Mapped[int] = mapped_column(index=True)
    conversation_id: Mapped[int] = mapped_column(index=True)
    #: Deliberately still globally unique, not tenant-scoped.
    #:
    #: This is the reserve-before-send anchor. claim_inbound and reserve_reply
    #: are single INSERT ... ON CONFLICT statements whose conflict target is
    #: this one column, and that is what makes a duplicate webhook delivery a
    #: no-op rather than a second send. Adding tenant_id to the key would
    #: change the conflict target, and a missed conflict here means a customer
    #: receives the same message twice. Meta's ids are globally unique in any
    #: case, so the global key is also the honest description of them.
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
    #:
    #: Also left global, for the reason above and because the comment-to-DM
    #: reservation shares this index. Scoping it is D7, deferred until
    #: integration ownership exists, because a wrong change produces duplicate
    #: sends to real customers.
    reply_to_wa_message_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    direction: Mapped[str] = mapped_column(String(10))  # "inbound" | "outbound"
    type: Mapped[str] = mapped_column(String(20), default="text")
    content: Mapped[str | None] = mapped_column(Text)
    media_id: Mapped[str | None] = mapped_column(String(128))
    # pending/sent/unconfirmed/delivered/read/failed
    status: Mapped[str | None] = mapped_column(String(20))
    #: The operator who typed this, for replies sent by a person from the
    #: dashboard. NULL for every inbound customer message and everything the
    #: bot sent, which is the overwhelming majority of rows -- so NULL means
    #: "no person sent this", not "we lost track of who did".
    #:
    #: ON DELETE SET NULL: removing an operator account must never remove the
    #: transcript a customer was part of.
    operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("operators.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
