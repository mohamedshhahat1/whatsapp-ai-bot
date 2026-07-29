"""Bot/human ownership on conversations.

Three columns rather than a new ``status`` value, and that distinction is
load-bearing.

``status`` already carries meaning that other objects depend on: the partial
unique index ``uq_active_conversation_per_user`` is defined
``WHERE status = 'active'``, and ``ConversationRepository.active_for_user``
filters on the same value. A ``status = 'handoff'`` row would therefore be
invisible to ``active_for_user``, so the next inbound message would open a
second conversation for a customer who already had one -- splitting the
transcript and letting the bot answer in a thread the operator was not
watching.

Lifecycle (open / archived) and ownership (bot / human) are independent facts,
so they get independent columns. A conversation stays ``active`` for its whole
handoff.

``server_default='bot'`` (not just a Python-side default) matters twice: it
backfills every existing row in this migration, and
``get_or_create_active`` inserts through ``pg_insert`` without listing ``mode``,
which bypasses ORM defaults entirely.

Revision ID: 0004_conversation_handoff
Revises: 0003_search_and_concurrency
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_conversation_handoff"
down_revision = "0003_search_and_concurrency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("mode", sa.String(16), nullable=False, server_default="bot"),
    )
    op.add_column(
        "conversations",
        sa.Column("assigned_operator", sa.String(64), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("handoff_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The dashboard filters "conversations waiting for a human" on this.
    op.create_index("ix_conversations_mode", "conversations", ["mode"])


def downgrade() -> None:
    op.drop_index("ix_conversations_mode", table_name="conversations")
    op.drop_column("conversations", "handoff_at")
    op.drop_column("conversations", "assigned_operator")
    op.drop_column("conversations", "mode")
