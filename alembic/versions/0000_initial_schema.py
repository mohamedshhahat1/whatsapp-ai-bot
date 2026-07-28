"""Baseline schema: users, conversations, messages, ai_logs.

The four core tables previously had no migration at all -- deployment
documentation told operators to run ``alembic revision --autogenerate`` against
a live database, which is not reproducible and cannot be reviewed. This
revision captures exactly what the ORM models in ``app/models`` declare, so a
fresh database is provisioned by ``alembic upgrade head`` alone.

No ``chat_sessions`` table is created: the model that described it was never
read or written by any service, repository, router or task, and has been
deleted rather than migrated into production.

Revision ID: 0000_initial_schema
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0000_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("wa_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_wa_id", "users", ["wa_id"], unique=True)

    op.create_table(
        "conversations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
    op.create_index("ix_conversations_status", "conversations", ["status"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("wa_message_id", sa.String(length=128), nullable=True),
        sa.Column("direction", sa.String(length=10), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("media_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.create_index(
        "ix_messages_wa_message_id", "messages", ["wa_message_id"], unique=True
    )
    op.create_index("ix_messages_created_at", "messages", ["created_at"])

    op.create_table(
        "ai_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_logs_conversation_id", "ai_logs", ["conversation_id"])
    op.create_index("ix_ai_logs_created_at", "ai_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_ai_logs_created_at", table_name="ai_logs")
    op.drop_index("ix_ai_logs_conversation_id", table_name="ai_logs")
    op.drop_table("ai_logs")

    op.drop_index("ix_messages_created_at", table_name="messages")
    op.drop_index("ix_messages_wa_message_id", table_name="messages")
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    op.drop_table("messages")

    op.drop_index("ix_conversations_status", table_name="conversations")
    op.drop_index("ix_conversations_user_id", table_name="conversations")
    op.drop_table("conversations")

    op.drop_index("ix_users_wa_id", table_name="users")
    op.drop_table("users")
