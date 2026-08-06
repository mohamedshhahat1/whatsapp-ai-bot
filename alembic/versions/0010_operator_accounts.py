"""Operator accounts, authenticated sessions and an append-only audit log.

Revision ID: 0010_operator_accounts
Revises: 0009_channel_identity

Three new tables and nothing else. No existing column is altered and no
existing row is rewritten, so this migration is safe to apply ahead of the
code that uses it -- which is how it is meant to be deployed.

The audit table is append-only, and enforced as such by a trigger rather than
by convention. Application-level discipline is not immutability: anyone with
the connection string can UPDATE a row, and the whole value of the table is
that its contents cannot be quietly adjusted afterwards.

TRUNCATE deliberately still works. Row-level triggers do not fire for it, so
it remains available as the retention path -- and it requires ownership of
the table rather than the INSERT the application role needs, which is the
right split of authority.

The revision id is kept short: alembic_version.version_num is VARCHAR(32),
and a longer id fails at 'alembic upgrade head' with
StringDataRightTruncation -- after the migration has already run.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_operator_accounts"
down_revision = "0009_channel_identity"
branch_labels = None
depends_on = None

# Match app.models.operator.LEGACY_OPERATOR_USERNAME and
# app.core.passwords.UNUSABLE_PASSWORD_HASH. Spelled literally rather than
# imported, on the same principle as 0009: a migration has to keep describing
# what it did even if the constant is renamed later.
LEGACY_USERNAME = "legacy-api-key"
UNUSABLE_PASSWORD_HASH = "!"


def upgrade() -> None:
    # --- operators ----------------------------------------------------------
    op.create_table(
        "operators",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "is_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    op.create_index("ix_operators_username", "operators", ["username"], unique=True)

    # --- operator_sessions --------------------------------------------------
    op.create_table(
        "operator_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "operator_id",
            sa.Integer(),
            sa.ForeignKey("operators.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # 64 hex characters of SHA-256. The token itself is never stored.
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.String(length=256), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_operator_sessions_operator_id", "operator_sessions", ["operator_id"]
    )
    op.create_index(
        "ix_operator_sessions_token_hash",
        "operator_sessions",
        ["token_hash"],
        unique=True,
    )
    # Expiry is swept in bulk, so it is looked up on its own as well as by id.
    op.create_index(
        "ix_operator_sessions_expires_at", "operator_sessions", ["expires_at"]
    )

    # --- audit_logs ---------------------------------------------------------
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "operator_id",
            sa.Integer(),
            sa.ForeignKey("operators.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=48), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_audit_logs_operator_id", "audit_logs", ["operator_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
    # "What happened to conversation 41" is the question this table is opened
    # for most often, and neither half of the pair answers it alone.
    op.create_index(
        "ix_audit_logs_resource", "audit_logs", ["resource_type", "resource_id"]
    )

    # The message is static on purpose: a '%' in RAISE EXCEPTION is a format
    # placeholder to PL/pgSQL and a parameter marker to the driver carrying
    # this statement, and the two disagree about what it means.
    op.execute(
        "CREATE OR REPLACE FUNCTION audit_logs_immutable() RETURNS trigger AS "
        "$func$ BEGIN RAISE EXCEPTION "
        "'audit_logs is append-only; UPDATE and DELETE are blocked'; "
        "END; $func$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER audit_logs_no_change BEFORE UPDATE OR DELETE ON "
        "audit_logs FOR EACH ROW EXECUTE FUNCTION audit_logs_immutable()"
    )

    # --- the reserved legacy account ---------------------------------------
    # Every request authenticated with the shared ADMIN_API_KEY is attributed
    # here, so that operator_id can be NOT NULL from the first row onwards
    # without withdrawing a credential the mobile client still depends on.
    op.execute(
        "INSERT INTO operators "
        "(username, display_name, password_hash, is_active, is_admin) "
        "VALUES ('legacy-api-key', 'Legacy shared API key', '!', true, true)"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_change ON audit_logs")
    op.execute("DROP FUNCTION IF EXISTS audit_logs_immutable()")

    op.drop_index("ix_audit_logs_resource", table_name="audit_logs")
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_index("ix_audit_logs_operator_id", table_name="audit_logs")
    op.drop_table("audit_logs")

    op.drop_index(
        "ix_operator_sessions_expires_at", table_name="operator_sessions"
    )
    op.drop_index(
        "ix_operator_sessions_token_hash", table_name="operator_sessions"
    )
    op.drop_index(
        "ix_operator_sessions_operator_id", table_name="operator_sessions"
    )
    op.drop_table("operator_sessions")

    op.drop_index("ix_operators_username", table_name="operators")
    op.drop_table("operators")
