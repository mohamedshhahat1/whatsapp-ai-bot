"""Finish the expand/contract move to (channel, external_id) identity.

Revision ID: 0013_external_id_not_null
Revises: 0012_audit_retention

0009_channel_identity added ``users.channel`` and ``users.external_id`` and
left the latter nullable on purpose: the WhatsApp writer still populated
``wa_id`` alone, and requiring a column nobody filled would have broken
customer creation on the first inbound message.

This is the contract half. Every existing row that has a phone number gets it
copied into ``external_id``, and the column is then tightened to NOT NULL, so
identity is (channel, external_id) for every row rather than for some of them.
``UserRepository.get_or_create`` is updated in the same commit, because the
migration and the writer are only correct together.

A row with neither id would fail the ALTER. That is deliberate: such a row
describes a customer the platform cannot address on any channel, and quietly
inventing an id for it would attach somebody's history to a fiction. The
backfill covers every row that carries a wa_id, which is every row 0009 could
have produced.

``wa_id`` stays. It is still WhatsApp's own identifier, still uniquely
indexed, and the phone number is what operators search by.
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_external_id_not_null"
down_revision = "0012_audit_retention"
branch_labels = None
depends_on = None

# channel is not filtered on: 0009 gave it a server-side default of
# 'whatsapp', so any row carrying a wa_id is a WhatsApp row by construction.
BACKFILL = """
UPDATE users
SET external_id = wa_id
WHERE external_id IS NULL
  AND wa_id IS NOT NULL
"""


def upgrade() -> None:
    op.execute(BACKFILL)
    op.alter_column(
        "users",
        "external_id",
        existing_type=sa.String(64),
        nullable=False,
    )


def downgrade() -> None:
    # The backfilled values are deliberately left in place. They are correct
    # data; the column simply stops requiring them. Clearing them on the way
    # down would also destroy the Messenger and Instagram ids, which were
    # never optional in practice and cannot be reconstructed from wa_id.
    op.alter_column(
        "users",
        "external_id",
        existing_type=sa.String(64),
        nullable=True,
    )
