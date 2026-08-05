"""Per-channel identity for users, and a channel on every conversation.

Revision ID: 0009_channel_identity
Revises: 0008_device_tokens

Expand/contract, deliberately stopping after the expand half.

Every writer in the codebase still creates users with wa_id alone. Adding
external_id as NOT NULL here would therefore break user creation on the first
inbound message after deploy -- so the column lands nullable, gets backfilled
from wa_id, and is tightened in the migration that ships alongside the
updated writers. A schema that is briefly looser than its final shape is a
better trade than an outage.

wa_id becomes nullable because a Messenger PSID or an Instagram IGSID is not
a phone number and there is nothing honest to put there. The existing unique
index on it is left untouched: Postgres permits any number of NULLs in a
unique index, so WhatsApp keeps its uniqueness guarantee and its index while
the other channels leave the column empty.

uq_active_conversation_per_user is deliberately NOT touched. It means one
active conversation per user row, and user rows are now per-channel, so it
already reads as one active conversation per person per channel. Rewriting
the partial unique index that the concurrency fix depends on, to arrive at
the behaviour it already has, would be risk taken for nothing.

The revision id is kept short: alembic_version.version_num is VARCHAR(32),
and a longer id fails at 'alembic upgrade head' with
StringDataRightTruncation -- after the migration has already run.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_channel_identity"
down_revision = "0008_device_tokens"
branch_labels = None
depends_on = None

# Matches app.channels.constants.WHATSAPP. Spelled literally rather than
# imported: a migration has to keep describing what it did even if the
# constant is renamed later.
WHATSAPP = "whatsapp"


def upgrade() -> None:
    # --- users --------------------------------------------------------------
    # server_default fills every existing row in place, so there is no window
    # where the column exists and is empty.
    op.add_column(
        "users",
        sa.Column(
            "channel",
            sa.String(length=24),
            nullable=False,
            server_default=WHATSAPP,
        ),
    )
    # Nullable on purpose -- see the module docstring.
    op.add_column(
        "users", sa.Column("external_id", sa.String(length=64), nullable=True)
    )
    op.execute(
        "UPDATE users SET external_id = wa_id WHERE external_id IS NULL"
    )

    # A Messenger or Instagram user has no phone number.
    op.alter_column(
        "users", "wa_id", existing_type=sa.String(length=32), nullable=True
    )

    op.create_index("ix_users_channel", "users", ["channel"])
    op.create_index("ix_users_external_id", "users", ["external_id"])
    # The real identity going forward. Rows with a NULL external_id do not
    # collide under this constraint, which is what makes the loose phase safe.
    op.create_unique_constraint(
        "uq_users_channel_external_id", "users", ["channel", "external_id"]
    )

    # --- conversations ------------------------------------------------------
    op.add_column(
        "conversations",
        sa.Column(
            "channel",
            sa.String(length=24),
            nullable=False,
            server_default=WHATSAPP,
        ),
    )
    # Indexed because the dashboard's first action is to filter by it.
    op.create_index("ix_conversations_channel", "conversations", ["channel"])


def downgrade() -> None:
    op.drop_index("ix_conversations_channel", table_name="conversations")
    op.drop_column("conversations", "channel")

    op.drop_constraint("uq_users_channel_external_id", "users", type_="unique")
    op.drop_index("ix_users_external_id", table_name="users")
    op.drop_index("ix_users_channel", table_name="users")

    # Only succeeds if no non-WhatsApp users were created while this was
    # applied -- they have no wa_id to restore, and inventing one would be
    # worse than refusing. Delete them first if you really mean to go back.
    op.alter_column(
        "users", "wa_id", existing_type=sa.String(length=32), nullable=False
    )
    op.drop_column("users", "external_id")
    op.drop_column("users", "channel")
