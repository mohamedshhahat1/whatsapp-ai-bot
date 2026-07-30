"""Tag conversations so sales leads surface on their own.

A lead is only worth catching if somebody sees it while the customer is
still typing. Until now every handoff looked identical in the operator
list -- a customer who said 'call me, I want to start next week' sat in
the same undifferentiated stream as one who had a complaint, ordered by
last activity, and was found by scrolling.

``tag`` is free text rather than an enum type. There will be more of
these (complaint, follow_up, after_sales), and adding a value to a
Postgres enum inside a transaction is a migration each time for no
integrity that the application is not already enforcing.

Indexed because the operator list sorts on it on every page load.

Revision ID: 0005_conversation_tag
Revises: 0004_conversation_handoff
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_conversation_tag"
down_revision = "0004_conversation_handoff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable with no server default: an untagged conversation is the
    # normal case, and NULL says 'nothing classified this' rather than
    # inventing a 'general' bucket that means the same thing but has to be
    # filtered out of every query.
    op.add_column(
        "conversations",
        sa.Column("tag", sa.String(length=32), nullable=True),
    )
    op.create_index("ix_conversations_tag", "conversations", ["tag"])


def downgrade() -> None:
    op.drop_index("ix_conversations_tag", table_name="conversations")
    op.drop_column("conversations", "tag")
