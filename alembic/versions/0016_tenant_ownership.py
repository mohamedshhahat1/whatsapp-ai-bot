"""Attach existing business data to tenants.

Revision ID: 0016_tenant_ownership
Revises: 0015_tenancy_foundation

Phase 1b. 0015 drew the boundary -- ``tenants`` and ``tenant_memberships`` --
without moving any business data behind it. This migration does that. The
approved tenant-owned tables gain ``tenant_id``, the uniqueness that was
deployment-global becomes tenant-scoped, and the parent foreign keys widen to
include the tenant, so that the database itself refuses a row whose parent
belongs to somebody else.

Scope, and what is deliberately left alone
------------------------------------------
Application-wide tenant scoping is Phase 1c. The only application changes
accompanying this migration are the writers that could not otherwise satisfy
a NOT NULL column. Read paths stay unscoped on purpose: isolation that is
half applied is harder to audit than isolation that is openly absent.

Untouched here: ``device_tokens`` (device ownership belongs to the
notification phase), ``operators`` and ``operator_sessions`` (login identity
is global by decision), ``model_pricing`` and ``alembic_version`` (properties
of the deployment, not of any tenant).

Ownership propagates parent before child
----------------------------------------
Customers, documents and rollup rows attach to the default tenant.
Conversations inherit from their customer, messages from their conversation,
chunks from their document, AI logs from their conversation where they still
have one. Nothing is attached by guesswork: every child copies a value the
previous statement already committed.

``audit_logs`` is the exception, and not by preference. 0010 installs
``audit_logs_no_change``, a row-level BEFORE UPDATE OR DELETE trigger, and
0012 rewrites its body so DELETE is permitted only while ``audit.allow_purge``
is on while UPDATE raises unconditionally. ADD COLUMN is DDL and does not fire
it; a backfill UPDATE would abort the migration. So the column is nullable,
historical rows keep NULL, and NULL reads as "recorded before tenancy, or
performed at platform level" -- which is a distinction this schema wants to
keep anyway. The trigger is neither weakened nor bypassed.

Why ai_logs keeps a single-column foreign key
---------------------------------------------
``ai_logs.conversation_id`` is ON DELETE SET NULL, and that is the whole
point of D5: deleting a conversation must not delete its cost record. Widening
that key to ``(tenant_id, conversation_id)`` would make Postgres null *both*
columns on parent delete, silently detaching the usage row from the tenant
that is billed for it. So ``ai_logs`` keeps its SET NULL key to conversations
and takes its own RESTRICT key to ``tenants`` instead.

Reversibility, and where it stops
---------------------------------
The downgrade restores the old schema exactly when the data still fits in it,
which means one tenant at most. With rows from two tenants the old schema has
nowhere to put the distinction: ``analytics_daily`` would need two rows under
one primary key, and two tenants' customers sharing a phone number would
collide on a global unique index. The downgrade therefore refuses, naming the
tenants it found. It never deletes, merges or rewrites a row to make itself
succeed.

Locks
-----
Each ADD COLUMN is metadata-only, but the backfill rewrites every row of
``messages`` and each constraint below holds ACCESS EXCLUSIVE for its
validation scan. On a populated deployment this is a maintenance-window
migration.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_tenant_ownership"
down_revision = "0015_tenancy_foundation"
branch_labels = None
depends_on = None

# The tenant 0015 creates for a deployment that predates tenancy. Spelled out
# rather than imported from app.models.tenant, because a migration has to keep
# working after the application constant it was written against has moved on.
DEFAULT_TENANT_SLUG = "default"

# Tables gaining a NOT NULL tenant_id, ordered the way ownership propagates.
OWNED_TABLES = (
    "users",
    "conversations",
    "messages",
    "documents",
    "document_chunks",
    "ai_logs",
    "analytics_daily",
)

# Tables taking a nullable tenant_id, where NULL is a meaningful value rather
# than missing data. See the module docstring on audit_logs.
NULLABLE_TENANT_TABLES = ("audit_logs",)

# Tables whose tenant is enforced through their parent's composite key instead
# of a direct key to tenants. A conversation cannot reference a tenant its
# customer does not belong to, so a second key would restate what the first
# already guarantees.
TENANT_VIA_PARENT = ("conversations", "messages", "document_chunks")


# --- Backfill ---------------------------------------------------------------
#
# Exposed as module constants so tests can execute the real statements. A test
# that restated the UPDATE would keep passing after somebody changed this one.

BACKFILL_USERS = """
UPDATE users SET tenant_id = :tenant_id WHERE tenant_id IS NULL
"""

BACKFILL_CONVERSATIONS = """
UPDATE conversations AS c
   SET tenant_id = u.tenant_id
  FROM users AS u
 WHERE u.id = c.user_id
   AND c.tenant_id IS NULL
"""

BACKFILL_MESSAGES = """
UPDATE messages AS m
   SET tenant_id = c.tenant_id
  FROM conversations AS c
 WHERE c.id = m.conversation_id
   AND m.tenant_id IS NULL
"""

BACKFILL_DOCUMENTS = """
UPDATE documents SET tenant_id = :tenant_id WHERE tenant_id IS NULL
"""

BACKFILL_DOCUMENT_CHUNKS = """
UPDATE document_chunks AS dc
   SET tenant_id = d.tenant_id
  FROM documents AS d
 WHERE d.id = dc.document_id
   AND dc.tenant_id IS NULL
"""

# Logs that still point at a conversation inherit from it, so usage already
# attributed to a tenant keeps that attribution exactly.
BACKFILL_AI_LOGS_VIA_CONVERSATION = """
UPDATE ai_logs AS l
   SET tenant_id = c.tenant_id
  FROM conversations AS c
 WHERE c.id = l.conversation_id
   AND l.tenant_id IS NULL
"""

# Logs whose conversation was already deleted, or that never had one. There is
# no evidence left of who they belonged to, so they go to the default tenant
# rather than being dropped or left unattributed.
BACKFILL_AI_LOGS_DETACHED = """
UPDATE ai_logs SET tenant_id = :tenant_id WHERE tenant_id IS NULL
"""

BACKFILL_ANALYTICS_DAILY = """
UPDATE analytics_daily SET tenant_id = :tenant_id WHERE tenant_id IS NULL
"""

# Anything the backfill failed to reach. Reported as a whole rather than one
# table at a time, so a failed migration names every problem at once.
UNBACKFILLED_ROWS = """
          SELECT 'users' AS source, count(*) AS remaining
            FROM users WHERE tenant_id IS NULL
UNION ALL SELECT 'conversations', count(*)
            FROM conversations WHERE tenant_id IS NULL
UNION ALL SELECT 'messages', count(*)
            FROM messages WHERE tenant_id IS NULL
UNION ALL SELECT 'documents', count(*)
            FROM documents WHERE tenant_id IS NULL
UNION ALL SELECT 'document_chunks', count(*)
            FROM document_chunks WHERE tenant_id IS NULL
UNION ALL SELECT 'ai_logs', count(*)
            FROM ai_logs WHERE tenant_id IS NULL
UNION ALL SELECT 'analytics_daily', count(*)
            FROM analytics_daily WHERE tenant_id IS NULL
"""

# Whether there is anything to attach at all. An empty database needs no
# tenant and no backfill, which is the path CI and a fresh install take.
POPULATED_OWNED_TABLES = """
          SELECT 'users' AS source WHERE EXISTS (SELECT 1 FROM users)
UNION ALL SELECT 'conversations' WHERE EXISTS (SELECT 1 FROM conversations)
UNION ALL SELECT 'messages' WHERE EXISTS (SELECT 1 FROM messages)
UNION ALL SELECT 'documents' WHERE EXISTS (SELECT 1 FROM documents)
UNION ALL SELECT 'document_chunks' WHERE EXISTS (SELECT 1 FROM document_chunks)
UNION ALL SELECT 'ai_logs' WHERE EXISTS (SELECT 1 FROM ai_logs)
UNION ALL SELECT 'analytics_daily' WHERE EXISTS (SELECT 1 FROM analytics_daily)
"""

# Every tenant that owns a row anywhere, including the audit trail. UNION
# rather than UNION ALL: the question is how many distinct tenants exist, and
# the downgrade can only represent one.
TENANT_IDS_IN_USE = """
      SELECT DISTINCT tenant_id FROM users WHERE tenant_id IS NOT NULL
UNION SELECT DISTINCT tenant_id FROM conversations WHERE tenant_id IS NOT NULL
UNION SELECT DISTINCT tenant_id FROM messages WHERE tenant_id IS NOT NULL
UNION SELECT DISTINCT tenant_id FROM documents WHERE tenant_id IS NOT NULL
UNION SELECT DISTINCT tenant_id FROM document_chunks WHERE tenant_id IS NOT NULL
UNION SELECT DISTINCT tenant_id FROM ai_logs WHERE tenant_id IS NOT NULL
UNION SELECT DISTINCT tenant_id FROM analytics_daily WHERE tenant_id IS NOT NULL
UNION SELECT DISTINCT tenant_id FROM audit_logs WHERE tenant_id IS NOT NULL
"""

DEFAULT_TENANT = "SELECT id FROM tenants WHERE slug = :slug"
ANY_TENANT = "SELECT id FROM tenants ORDER BY id LIMIT 1"

# Which kind of object a name refers to. 0000 and 0001 created some of the
# uniqueness in this schema as unique indexes and some as constraints, and the
# two are dropped by different commands. Asking the catalogue is cheaper than
# being wrong.
IS_UNIQUE_CONSTRAINT = """
SELECT 1
  FROM pg_constraint AS con
  JOIN pg_class AS rel ON rel.oid = con.conrelid
 WHERE con.contype = 'u'
   AND con.conname = :name
   AND rel.relname = :table
"""

# The name Postgres gave a single-column foreign key that was created without
# one. Looked up rather than assumed, so this migration does not depend on the
# server's naming convention staying what it is today.
SINGLE_COLUMN_FK_NAME = """
SELECT con.conname
  FROM pg_constraint AS con
  JOIN pg_class AS rel ON rel.oid = con.conrelid
  JOIN pg_namespace AS nsp ON nsp.oid = rel.relnamespace
  JOIN pg_attribute AS att
    ON att.attrelid = rel.oid
   AND att.attnum = con.conkey[1]
 WHERE con.contype = 'f'
   AND rel.relname = :table
   AND att.attname = :column
   AND array_length(con.conkey, 1) = 1
   AND nsp.nspname = current_schema()
 LIMIT 1
"""


def _default_tenant_id(bind: sa.engine.Connection) -> int | None:
    """The tenant existing data belongs to, or None on an empty deployment.

    Prefers the slug 0015 seeds. Falls back to the lowest tenant id so that a
    deployment which renamed its first tenant still migrates, rather than
    failing on a cosmetic difference.
    """
    row = bind.execute(sa.text(DEFAULT_TENANT), {"slug": DEFAULT_TENANT_SLUG}).first()
    if row is None:
        row = bind.execute(sa.text(ANY_TENANT)).first()
    return None if row is None else int(row[0])


def _backfill(bind: sa.engine.Connection) -> None:
    """Attach every existing row to a tenant, parents before children."""
    tenant_id = _default_tenant_id(bind)

    if tenant_id is None:
        populated = [row[0] for row in bind.execute(sa.text(POPULATED_OWNED_TABLES))]
        if populated:
            raise RuntimeError(
                "No tenant exists, but these tables hold rows that must belong "
                "to one: " + ", ".join(populated) + ". Migration 0015 creates "
                "the default tenant; run it first, or insert a tenant "
                "deliberately before upgrading."
            )
        # Nothing to attach. Every table below is empty, so NOT NULL applies
        # trivially and a fresh install pays none of this cost.
        return

    params = {"tenant_id": tenant_id}
    bind.execute(sa.text(BACKFILL_USERS), params)
    bind.execute(sa.text(BACKFILL_CONVERSATIONS))
    bind.execute(sa.text(BACKFILL_MESSAGES))
    bind.execute(sa.text(BACKFILL_DOCUMENTS), params)
    bind.execute(sa.text(BACKFILL_DOCUMENT_CHUNKS))
    bind.execute(sa.text(BACKFILL_AI_LOGS_VIA_CONVERSATION))
    bind.execute(sa.text(BACKFILL_AI_LOGS_DETACHED), params)
    bind.execute(sa.text(BACKFILL_ANALYTICS_DAILY), params)


def _assert_backfilled(bind: sa.engine.Connection) -> None:
    """Fail before SET NOT NULL rather than during it.

    SET NOT NULL would abort on its own, but its error names a column and not
    the reason, and on a large table it aborts only after a full scan. This
    reports every affected table together, in the language of the data.
    """
    remaining = [
        f"{row[0]} ({row[1]} rows)"
        for row in bind.execute(sa.text(UNBACKFILLED_ROWS))
        if row[1]
    ]
    if remaining:
        raise RuntimeError(
            "Tenant backfill left rows unattached: "
            + ", ".join(remaining)
            + ". This means a child row points at a parent that no longer "
            "exists; resolve the orphans before upgrading. No data was changed."
        )


def _drop_unique(bind: sa.engine.Connection, table: str, name: str) -> None:
    """Drop a unique constraint or a unique index, whichever ``name`` is."""
    is_constraint = bind.execute(
        sa.text(IS_UNIQUE_CONSTRAINT), {"name": name, "table": table}
    ).first()
    if is_constraint is not None:
        op.drop_constraint(name, table, type_="unique")
    else:
        op.drop_index(name, table_name=table)


def _widen_parent_fk(
    bind: sa.engine.Connection,
    *,
    child: str,
    column: str,
    parent: str,
    name: str,
    ondelete: str,
) -> None:
    """Replace a single-column parent key with a tenant-carrying composite one.

    The ON DELETE behaviour is passed through unchanged. The point of the
    composite key is not to alter what a delete does; it is that a child can no
    longer name a parent in another tenant, which the application would
    otherwise have to be trusted to prevent on every write path.
    """
    existing = bind.execute(
        sa.text(SINGLE_COLUMN_FK_NAME), {"table": child, "column": column}
    ).first()
    if existing is not None:
        op.drop_constraint(str(existing[0]), child, type_="foreignkey")
    op.create_foreign_key(
        name,
        child,
        parent,
        ["tenant_id", column],
        ["tenant_id", "id"],
        ondelete=ondelete,
    )


def upgrade() -> None:
    bind = op.get_bind()

    # Expand: nullable everywhere first, so the backfill has somewhere to
    # write and nothing is rejected while it runs.
    for table in (*OWNED_TABLES, *NULLABLE_TENANT_TABLES):
        op.add_column(table, sa.Column("tenant_id", sa.Integer(), nullable=True))

    _backfill(bind)
    _assert_backfilled(bind)

    # Contract: only the tables where NULL would mean missing data.
    for table in OWNED_TABLES:
        op.alter_column(table, "tenant_id", existing_type=sa.Integer(), nullable=False)

    # analytics_daily is excluded: its primary key below leads with tenant_id,
    # and a second index on the same leading column would only cost writes.
    for table in (*OWNED_TABLES, *NULLABLE_TENANT_TABLES):
        if table == "analytics_daily":
            continue
        op.create_index(f"ix_{table}_tenant_id", table, ["tenant_id"])

    # RESTRICT throughout. A tenant with data is closed or suspended, never
    # deleted out from under its own history.
    for table in (*OWNED_TABLES, *NULLABLE_TENANT_TABLES):
        if table in TENANT_VIA_PARENT:
            continue
        op.create_foreign_key(
            f"fk_{table}_tenant",
            table,
            "tenants",
            ["tenant_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    # The keys the composite child foreign keys point at. A plain primary key
    # on id cannot serve them, because a foreign key must reference a unique
    # constraint over exactly the columns it names.
    for table in ("users", "conversations", "documents"):
        op.create_unique_constraint(
            f"uq_{table}_tenant_scoped_id", table, ["tenant_id", "id"]
        )

    _widen_parent_fk(
        bind,
        child="conversations",
        column="user_id",
        parent="users",
        name="fk_conversations_user",
        ondelete="CASCADE",
    )
    _widen_parent_fk(
        bind,
        child="messages",
        column="conversation_id",
        parent="conversations",
        name="fk_messages_conversation",
        ondelete="CASCADE",
    )
    _widen_parent_fk(
        bind,
        child="document_chunks",
        column="document_id",
        parent="documents",
        name="fk_document_chunks_document",
        ondelete="CASCADE",
    )

    # Uniqueness becomes tenant-scoped. Both customer constraints change in
    # this one step, per D2: scoping either alone leaves the other free to
    # swallow a second tenant's insert and hand back the first tenant's row.
    #
    # ix_users_wa_id keeps its name and loses only its uniqueness. The phone
    # number is still what operators search by, so the lookup index stays.
    _drop_unique(bind, "users", "ix_users_wa_id")
    op.create_index("ix_users_wa_id", "users", ["wa_id"])
    op.create_unique_constraint(
        "uq_users_tenant_wa_id", "users", ["tenant_id", "wa_id"]
    )

    # Recreated under the same name, and as a constraint rather than an index,
    # because UserRepository.get_or_create_by_channel names it in ON CONFLICT
    # ON CONSTRAINT and only a real constraint can be named there.
    _drop_unique(bind, "users", "uq_users_channel_external_id")
    op.create_unique_constraint(
        "uq_users_channel_external_id",
        "users",
        ["tenant_id", "channel", "external_id"],
    )

    _drop_unique(bind, "documents", "ix_documents_source")
    op.create_index("ix_documents_source", "documents", ["source"])
    op.create_unique_constraint(
        "uq_documents_tenant_source", "documents", ["tenant_id", "source"]
    )

    # The rollup's idempotence is its primary key: re-running a night collides
    # and updates in place. With tenants, the thing that must collide is the
    # pair, so the key becomes the pair.
    op.drop_constraint("pk_analytics_daily", "analytics_daily", type_="primary")
    op.create_primary_key(
        "pk_analytics_daily", "analytics_daily", ["tenant_id", "day"]
    )


def downgrade() -> None:
    bind = op.get_bind()

    tenants = [int(row[0]) for row in bind.execute(sa.text(TENANT_IDS_IN_USE))]
    if len(tenants) > 1:
        raise RuntimeError(
            "Refusing to downgrade: rows belong to "
            + str(len(tenants))
            + " tenants ("
            + ", ".join(str(tenant) for tenant in sorted(tenants))
            + "). The pre-1b schema has no column to keep them apart -- "
            "analytics_daily would need two rows under one primary key, and "
            "two tenants' customers sharing a phone number would collide on a "
            "global unique index. Export or remove the extra tenants' data "
            "deliberately first. Nothing has been changed."
        )

    # Reverse of upgrade, last thing first. Every uniqueness restored below is
    # satisfiable precisely because the guard above proved a single tenant.
    op.drop_constraint("pk_analytics_daily", "analytics_daily", type_="primary")
    op.create_primary_key("pk_analytics_daily", "analytics_daily", ["day"])

    op.drop_constraint("uq_documents_tenant_source", "documents", type_="unique")
    op.drop_index("ix_documents_source", table_name="documents")
    op.create_index("ix_documents_source", "documents", ["source"], unique=True)

    op.drop_constraint("uq_users_channel_external_id", "users", type_="unique")
    op.create_unique_constraint(
        "uq_users_channel_external_id", "users", ["channel", "external_id"]
    )

    op.drop_constraint("uq_users_tenant_wa_id", "users", type_="unique")
    op.drop_index("ix_users_wa_id", table_name="users")
    op.create_index("ix_users_wa_id", "users", ["wa_id"], unique=True)

    # Restored without an explicit name so Postgres reissues the same default
    # it chose in 0000, leaving a re-upgrade to find exactly what it expects.
    op.drop_constraint(
        "fk_document_chunks_document", "document_chunks", type_="foreignkey"
    )
    op.create_foreign_key(
        None,
        "document_chunks",
        "documents",
        ["document_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("fk_messages_conversation", "messages", type_="foreignkey")
    op.create_foreign_key(
        None,
        "messages",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("fk_conversations_user", "conversations", type_="foreignkey")
    op.create_foreign_key(
        None, "conversations", "users", ["user_id"], ["id"], ondelete="CASCADE"
    )

    for table in ("users", "conversations", "documents"):
        op.drop_constraint(f"uq_{table}_tenant_scoped_id", table, type_="unique")

    for table in (*OWNED_TABLES, *NULLABLE_TENANT_TABLES):
        if table in TENANT_VIA_PARENT:
            continue
        op.drop_constraint(f"fk_{table}_tenant", table, type_="foreignkey")

    for table in (*OWNED_TABLES, *NULLABLE_TENANT_TABLES):
        if table == "analytics_daily":
            continue
        op.drop_index(f"ix_{table}_tenant_id", table_name=table)

    # Dropping the column is the only data this downgrade removes, and it
    # removes exactly the attribution the upgrade added -- never a business
    # row. audit_logs is included: DDL does not fire the append-only trigger.
    for table in (*OWNED_TABLES, *NULLABLE_TENANT_TABLES):
        op.drop_column(table, "tenant_id")
