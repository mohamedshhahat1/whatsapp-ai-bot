"""Make one inbound message answerable exactly once.

Retries are the whole point of the task queue, and until now they were only
half safe. ``messages.wa_message_id`` is unique, so an inbound message could
not be stored twice -- but the AI reply to it had no such anchor. A worker
that died after calling OpenAI and sending to WhatsApp, but before its
transaction committed, left nothing behind: the retry found no inbound row,
processed the message again, and the customer got a second reply on a second
invoice.

``reply_to_wa_message_id`` gives the outbound message the same guarantee the
inbound one already had. The unique index is the enforcement -- the
application reserves the row before it calls WhatsApp, so a concurrent or
retried attempt loses the insert and knows not to send.

Nullable because most outbound messages are not answers to a specific inbound
message: operator replies sent from the dashboard, and the fixed copy the bot
sends without a model call, have no inbound id to point at. Postgres allows
unlimited NULLs in a unique index, so those rows do not contend.

Revision ID: 0006_reply_idempotency
Revises: 0005_conversation_tag
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_reply_idempotency"
down_revision = "0005_conversation_tag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("reply_to_wa_message_id", sa.String(length=128), nullable=True),
    )

    # Unique rather than a plain index: this is a correctness constraint, not
    # a lookup optimisation. It is the thing that physically prevents two
    # replies to one customer message, and it holds even if a future caller
    # forgets to check first.
    op.create_unique_constraint(
        "uq_messages_reply_to_wa_message_id",
        "messages",
        ["reply_to_wa_message_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_messages_reply_to_wa_message_id", "messages", type_="unique"
    )
    op.drop_column("messages", "reply_to_wa_message_id")
