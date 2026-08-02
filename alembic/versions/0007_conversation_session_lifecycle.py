"""Give every conversation a session lifecycle: welcome, idle, closing.

Until now a conversation was open forever. The welcome fired once on the
customer's very first message and nothing ever ended it: someone who asked a
question in March and came back in July resumed the same thread, carrying the
same context window, and never saw a greeting again.

A session is the unit the customer actually experiences -- hello, some
questions, goodbye -- and these four timestamps are what let the schema hold
one.

``last_activity_at``
    When anything last happened here, in either direction. The idle timer
    counts from this, so it is written by inbound messages, AI replies,
    operator replies and mode switches alike. NOT NULL, because a null would
    read as "infinitely idle" to the sweeper and close the conversation on the
    first pass.

``welcome_sent_at``
    When the approved welcome copy actually reached the customer. Replaces
    counting inbound messages, which answered a subtly different question:
    a welcome whose send failed left the count at one, and the next message
    made it two, so that customer was never greeted at all.

``closing_sent_at``
    Claimed the moment a worker takes the right to say goodbye. This is the
    anti-duplicate anchor, and it is taken with a conditional UPDATE rather
    than a read followed by a write: several workers sweep concurrently, and
    check-then-set lets two of them close the same session.

``closed_at``
    When the session actually ended. Deliberately separate from
    ``closing_sent_at``, because a session can close without a closing
    message ever being sent -- the feature can be switched off, and a session
    that went idle outside WhatsApp's 24-hour service window cannot be sent
    anything at all. Collapsing the two would make "did we say goodbye?"
    unanswerable.

Why there is no new state column
--------------------------------
``status`` and ``mode`` already carry the four states this feature needs::

    status=active + mode=bot      ACTIVE_BOT
    status=active + mode=human    ACTIVE_HUMAN
    status=active + idle          WAITING_IDLE   (from last_activity_at)
    status=closed                 CLOSED

A third overlapping column would introduce combinations that contradict the
two it duplicates, with no trustworthy way to decide which is right.
WAITING_IDLE is derived rather than stored for the same reason: storing it
would need a writer to flip it on a timer, which is a second scheduled job
asserting something the timestamp already proves.

Why reopening needs no reset logic
----------------------------------
``uq_active_conversation_per_user`` from migration 0003 is partial on
``status = 'active'``. Closing a session therefore releases the customer's
slot, and the next inbound message creates a genuinely new conversation row
through the existing ``get_or_create_active``. Fresh history for the model, a
null ``welcome_sent_at`` and a null ``closing_sent_at`` all come for free --
there are no flags to clear, and so none to forget to clear.

Revision ID: 0007_conversation_session_lifecycle
Revises: 0006_reply_idempotency
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_conversation_session_lifecycle"
down_revision = "0006_reply_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Added nullable, backfilled, then made NOT NULL. Adding it NOT NULL with a
    # server default in one step would stamp every existing row with now().
    op.add_column(
        "conversations",
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("welcome_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("closing_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Backfill from updated_at, not now(). Stamping now() would present every
    # historical conversation to the sweeper as freshly active, and five
    # minutes after this migration ran the bot would say goodbye to every
    # customer it had ever spoken to -- months after they last wrote in.
    #
    # Dating them from their real last activity makes them long idle instead,
    # which is both true and safe: the sweeper refuses to send outside
    # WhatsApp's 24-hour service window, so these are closed silently.
    op.execute(
        "UPDATE conversations "
        "SET last_activity_at = COALESCE(updated_at, created_at, now())"
    )

    # Existing conversations have already greeted their customer, whatever the
    # message counts say. Leaving this null would greet all of them a second
    # time on their next message.
    op.execute(
        "UPDATE conversations "
        "SET welcome_sent_at = COALESCE(created_at, now()) "
        "WHERE EXISTS ("
        "    SELECT 1 FROM messages"
        "    WHERE messages.conversation_id = conversations.id"
        "      AND messages.direction = 'outbound'"
        ")"
    )

    op.alter_column(
        "conversations",
        "last_activity_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )

    # The sweeper's only query, and it runs on a schedule forever. Partial on
    # exactly the rows it can act on: an active session that has not already
    # been said goodbye to. Conversations that are closed, or already closed
    # out, stay out of the index entirely -- which is the bulk of the table
    # after a few months, and all of it after a year.
    op.create_index(
        "ix_conversations_idle_sweep",
        "conversations",
        ["last_activity_at"],
        postgresql_where=sa.text(
            "status = 'active' AND closing_sent_at IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_idle_sweep", table_name="conversations")
    op.drop_column("conversations", "closed_at")
    op.drop_column("conversations", "closing_sent_at")
    op.drop_column("conversations", "welcome_sent_at")
    op.drop_column("conversations", "last_activity_at")
