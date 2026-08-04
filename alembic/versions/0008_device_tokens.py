"""Device tokens for mobile push notifications.

Revision ID: 0008_device_tokens
Revises: 0007_session_lifecycle

One row per mobile device, keyed by its Firebase registration token.

No operator_id column, and no operators table: see the module docstring on
app/models/device_token.py. Authentication is a single shared admin key, so
there is nothing in this database that identifies a person to point a foreign
key at. Tokens are device-scoped and notifications fan out to every enabled
device.

The revision id is kept short on purpose. alembic_version.version_num is
VARCHAR(32), and a longer id fails at 'alembic upgrade head' with
StringDataRightTruncation -- after the migration has already run.
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_device_tokens"
down_revision = "0007_session_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token", sa.String(length=512), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column(
            "notification_privacy",
            sa.String(length=16),
            nullable=False,
            server_default="private",
        ),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("disabled_reason", sa.String(length=32), nullable=True),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("token", name="uq_device_tokens_token"),
    )
    op.create_index("ix_device_tokens_enabled", "device_tokens", ["enabled"])


def downgrade() -> None:
    op.drop_index("ix_device_tokens_enabled", table_name="device_tokens")
    op.drop_table("device_tokens")
