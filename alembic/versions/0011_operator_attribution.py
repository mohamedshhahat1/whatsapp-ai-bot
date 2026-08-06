"""Attribute replies and handoffs to operator accounts.

Revision ID: 0011_operator_attribution
Revises: 0010_operator_accounts

Two nullable foreign keys. No backfill: existing rows genuinely have no
known operator, and inventing one -- pointing them all at the reserved
legacy account, say -- would assert something the data does not support.
NULL means "no person did this", which for every row written before this
migration is either true or unknowable, and those are both better served
by NULL than by a plausible-looking wrong answer.

conversations.assigned_operator is deliberately left in place. It is
populated on every handed-off row, serialised by ConversationRead, and read
by both clients; removing it in the same migration that adds its successor
would turn a schema addition into a breaking API change.

Both columns are ON DELETE SET NULL. CASCADE would let removing an operator
account delete customer conversations and message history.
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_operator_attribution"
down_revision = "0010_operator_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "operator_id",
            sa.Integer(),
            sa.ForeignKey("operators.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    # Indexed because "what did this operator send" is the question it will be
    # asked, and because the FK check on operator deletion scans it.
    op.create_index("ix_messages_operator_id", "messages", ["operator_id"])

    op.add_column(
        "conversations",
        sa.Column(
            "assigned_operator_id",
            sa.Integer(),
            sa.ForeignKey("operators.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_conversations_assigned_operator_id",
        "conversations",
        ["assigned_operator_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversations_assigned_operator_id", table_name="conversations"
    )
    op.drop_column("conversations", "assigned_operator_id")
    op.drop_index("ix_messages_operator_id", table_name="messages")
    op.drop_column("messages", "operator_id")
