"""Permit retention deletes on audit_logs while keeping UPDATE blocked.

Revision ID: 0012_audit_retention
Revises: 0011_operator_attribution

Migration 0010 made audit_logs append-only with a BEFORE UPDATE OR DELETE row
trigger that raises unconditionally. That is the right default and it is also
why the table can never be trimmed: a retention policy needs to delete, and
the trigger does not distinguish expiring a record from destroying evidence.

Its docstring suggested TRUNCATE as the retention path, because row triggers
do not fire for it. That does not work here on two counts. TRUNCATE removes
every row, so it cannot express "keep the last N days" at all; and it requires
ownership of the table, which the application role deliberately does not have.

So the function is replaced with one that permits DELETE only while a
transaction-local setting is present, and refuses everything else exactly as
before. The purge sets that flag inside its own transaction with
set_config(..., true), so it is gone the moment the transaction ends and can
never leak into an ordinary request.

UPDATE remains categorically blocked. There is no flag that permits rewriting
a record, only one that permits expiring it, which is the distinction the
append-only guarantee is actually about.

The raise message is unchanged, so anything asserting on it still passes.

No per-cent signs and no colons appear in the SQL below: per-cent is a
PL/pgSQL format placeholder, and a colon is a driver parameter marker.
"""

from alembic import op

revision = "0012_audit_retention"
down_revision = "0011_operator_attribution"
branch_labels = None
depends_on = None

# Reads as: a delete is allowed only when the current transaction has said so.
# current_setting with the missing_ok flag returns NULL when nothing set it,
# and coalesce turns that into a plain 'off' rather than relying on how the
# IF treats a NULL condition.
GUARDED_FUNCTION = """
CREATE OR REPLACE FUNCTION audit_logs_immutable() RETURNS trigger AS
$func$
BEGIN
    IF TG_OP = 'DELETE'
       AND coalesce(current_setting('audit.allow_purge', true), 'off') = 'on'
    THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'audit_logs is append-only; UPDATE and DELETE are blocked';
END;
$func$ LANGUAGE plpgsql
"""

# Exactly the body migration 0010 installed.
UNCONDITIONAL_FUNCTION = """
CREATE OR REPLACE FUNCTION audit_logs_immutable() RETURNS trigger AS
$func$ BEGIN RAISE EXCEPTION
'audit_logs is append-only; UPDATE and DELETE are blocked';
END; $func$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    # The trigger itself is untouched: it already calls this function by name,
    # so replacing the body is the whole change.
    op.execute(GUARDED_FUNCTION)


def downgrade() -> None:
    op.execute(UNCONDITIONAL_FUNCTION)
