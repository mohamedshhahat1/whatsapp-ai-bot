"""Trigram search index and the active-conversation uniqueness guarantee.

Two unrelated-looking changes share a revision because both are pure index
work on existing tables:

* ``ix_messages_content_trgm`` makes ``/admin/search`` index-backed. The query
  uses a leading-wildcard ILIKE, which no B-tree can serve; a GIN index with
  ``gin_trgm_ops`` is the only structure Postgres can use for it.
* ``uq_active_conversation_per_user`` turns "one active conversation per
  customer" from an application convention into a database guarantee, which is
  what lets ConversationRepository.get_or_create_active use ON CONFLICT
  instead of a check-then-insert race.

Existing duplicate active conversations are archived first, keeping the most
recent one, otherwise the unique index could not be built.

Revision ID: 0003_search_and_concurrency
Revises: 0002_model_pricing
"""

from alembic import op

revision = "0003_search_and_concurrency"
down_revision = "0002_model_pricing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep the newest active conversation per user; archive the rest.
    op.execute(
        """
        UPDATE conversations AS c
        SET status = 'archived'
        WHERE c.status = 'active'
          AND c.id <> (
              SELECT c2.id
              FROM conversations AS c2
              WHERE c2.user_id = c.user_id
                AND c2.status = 'active'
              ORDER BY c2.created_at DESC, c2.id DESC
              LIMIT 1
          )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_active_conversation_per_user "
        "ON conversations (user_id) WHERE status = 'active'"
    )

    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX ix_messages_content_trgm "
        "ON messages USING gin (content gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_messages_content_trgm")
    op.execute("DROP INDEX IF EXISTS uq_active_conversation_per_user")
    # The pg_trgm extension is left in place: other objects may depend on it,
    # and dropping it is not reversible in the way the archived rows are not.
