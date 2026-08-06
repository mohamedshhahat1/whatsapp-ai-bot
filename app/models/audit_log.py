"""An append-only record of what each operator did.

One row per state-changing admin action. Reads are not recorded: an audit
table that also logs every list and every detail view fills with traffic
nobody investigates, and the rows that matter become harder to find rather
than easier.

Failed logins are deliberately absent too. There is no authenticated operator
when a password is wrong, so a row for one would need a nullable operator_id,
and that single nullable column would change what the table means -- from
"what an operator did" to "things that happened, some of them to nobody".
They are logged through structlog instead, where the rest of the security
signal already lives.

Immutability is enforced by the database. See
alembic/versions/0010_operator_accounts.py for the trigger.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Action vocabulary. Dotted "<resource>.<verb>" so that a prefix match reads
# naturally in a WHERE clause, and stable: these strings end up in stored rows
# and renaming one silently orphans the history it was recorded under.
ACTION_LOGIN = "operator.login"
ACTION_LOGOUT = "operator.logout"
ACTION_CONVERSATION_DELETE = "conversation.delete"
ACTION_CONVERSATION_TAKEOVER = "conversation.takeover"
ACTION_CONVERSATION_RESUME = "conversation.resume"
ACTION_CONVERSATION_REPLY = "conversation.reply"
ACTION_AI_TOGGLE = "ai.toggle"
ACTION_CUSTOMER_UNBLOCK = "customer.unblock"
ACTION_PRICING_CREATE = "pricing.create"
ACTION_PRICING_DELETE = "pricing.delete"

# What the action was performed on. SYSTEM covers the switches that belong to
# the deployment rather than to any one row, such as the global AI toggle.
RESOURCE_CONVERSATION = "conversation"
RESOURCE_CUSTOMER = "customer"
RESOURCE_OPERATOR = "operator"
RESOURCE_PRICING = "pricing"
RESOURCE_SYSTEM = "system"


class AuditLog(Base):
    """One recorded administrative action."""

    __tablename__ = "audit_logs"

    # BigInteger because this table only grows: it is never updated, and
    # pruning it is a deliberate operational act rather than routine.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # RESTRICT, not CASCADE or SET NULL. A trail that can lose its subject is
    # not a trail, so an operator who leaves is deactivated instead.
    operator_id: Mapped[int] = mapped_column(
        ForeignKey("operators.id", ondelete="RESTRICT"), index=True
    )
    action: Mapped[str] = mapped_column(String(48), index=True)
    resource_type: Mapped[str] = mapped_column(String(32))
    # Text rather than an integer: the affected resource is a conversation id
    # for some actions and a wa_id or a model name for others, and one column
    # that holds all of them beats three that are each usually null.
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Whatever the action needs to be understood later without joining to
    # rows that may since have changed or been deleted -- the text of a reply,
    # the new position of a switch, the operator label on a takeover.
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
