"""Tenancy foundation: the tenant boundary and who is allowed to own one.

Creates ``tenants`` and ``tenant_memberships``, then decides who owns the
tenant that existing data will be attached to.

Migration 0010 seeded a reserved ``legacy-api-key`` operator so that requests
authenticated with the shared ADMIN_API_KEY had an identity to be attributed
to. It is flagged ``is_admin`` and on a fresh deployment it is the only
administrative row there is, which makes it the obvious candidate for owner
and the wrong one. It is a single shared credential held by whoever holds the
environment file, it belongs to no person, and giving it a tenant role would
convert platform-level access into tenant-level access without anybody
deciding to. It is excluded twice over, by username and by password hash, and
a deployment whose only administrative identity is that key is refused.

An owner is never invented. Only an account the deployment already flagged
``is_admin`` may hold ``owner``: that flag is the sole existing record of "this
person administers this installation". Where no such account exists, no owner
is created rather than promoting whichever operator happens to have the lowest
id. Seniority is not authority.

Refusing is only right when there is something to own. On an empty database no
customer, conversation, message or document exists, so no ownership question
arises and proceeding cannot produce an ambiguous owner. CI migrates exactly
such a database from nothing on every run, and so does a restore from backup;
refusing there would make the chain unrunnable while protecting no data. The
refusal is therefore conditioned on tenant-ownable rows actually being
present. Empty database: create the tenant, attach nobody. Populated database
with no eligible administrator: abort with instructions, having written
nothing.

Both inserts are ``ON CONFLICT DO NOTHING`` on the constraint that defines the
row's identity, so repeating this step cannot duplicate the tenant or a
membership.

Deliberately out of scope, each for its own reason:

* No ``tenant_id`` column on any existing table. Rewriting ownership of live
  rows is a different operation with a different lock profile from creating
  two empty tables, and separating them keeps both independently reversible.
* No ``tenant_settings`` table. There is no setting to store in it yet, and a
  column invented ahead of its requirement is a column nobody validates.
* No ``tenant_invitations`` table. What matters about an invitation is its
  lifecycle, which is only testable alongside the flow that issues and
  accepts one.

Revision ID: 0015_tenancy_foundation
Revises: 0014_analytics_daily_rollup
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_tenancy_foundation"
down_revision: str | None = "0014_analytics_daily_rollup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Copied, not imported. A migration records what ran at one point in time; if
# it imported application constants, editing one later would retroactively
# change what this migration is understood to have done. The copies are pinned
# to their originals by tests/test_tenancy_foundation.py, which fails if the
# two ever drift apart.
UNUSABLE_PASSWORD_HASH = "!"
LEGACY_OPERATOR_USERNAME = "legacy-api-key"

DEFAULT_TENANT_SLUG = "default"
DEFAULT_TENANT_NAME = "Default"

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"

# The tables that gain tenant ownership in the migration after this one. A row
# in any of them is a row the tenant created here would end up owning, which
# is what makes the owner question real rather than theoretical.
TENANT_OWNABLE_TABLES = ("users", "conversations", "messages", "documents")

TENANT_OWNABLE_DATA_EXISTS = "SELECT " + " OR ".join(
    f"EXISTS (SELECT 1 FROM {table})" for table in TENANT_OWNABLE_TABLES
)

# Eligibility is the model's own ``can_log_in`` test, expressed in SQL, plus
# the reserved username. The hash check alone would be enough today -- the
# legacy row carries the sentinel -- but it would stop being enough the moment
# somebody set a real password on that account, and the exclusion is meant to
# be unconditional.
#
# Administrators first, then by id, which is creation order. The first row is
# therefore the longest-standing administrator whenever the deployment has
# one, and no other row is allowed to become owner.
ELIGIBLE_OPERATORS = """
SELECT id, is_admin
FROM operators
WHERE is_active = true
  AND password_hash <> :unusable_hash
  AND username <> :legacy_username
ORDER BY is_admin DESC, id ASC
"""

ELIGIBILITY_PARAMS = {
    "unusable_hash": UNUSABLE_PASSWORD_HASH,
    "legacy_username": LEGACY_OPERATOR_USERNAME,
}

# ON CONFLICT DO NOTHING on both inserts: re-running this step against a
# database that already has the default tenant must be a no-op rather than a
# unique-violation, so that a partially applied upgrade can be repeated.
INSERT_DEFAULT_TENANT = """
INSERT INTO tenants (name, slug, status)
VALUES (:name, :slug, 'active')
ON CONFLICT (slug) DO NOTHING
"""

SELECT_TENANT_BY_SLUG = "SELECT id FROM tenants WHERE slug = :slug"

INSERT_MEMBERSHIP = """
INSERT INTO tenant_memberships (tenant_id, operator_id, role)
VALUES (:tenant_id, :operator_id, :role)
ON CONFLICT (tenant_id, operator_id) DO NOTHING
"""

NO_ELIGIBLE_OWNER = f"""\
Cannot establish an owner for the default tenant.

This database already contains customer data, so the tenant this migration
creates would own it -- but no account exists that may hold the owner role.
An owner is never invented here: it has to be an active administrator account
that somebody created deliberately.

The only administrative identity present is the shared ADMIN_API_KEY operator
({LEGACY_OPERATOR_USERNAME!r}), which is a platform-level credential belonging
to no person and must never be given tenant membership.

Create a real administrator account first, then run this migration again:

    python -m app.cli create-admin

or, where the application runs in Docker:

    docker compose exec api python -m app.cli create-admin

An account may own a tenant when it is active, is flagged as an administrator,
carries a real password hash rather than the {UNUSABLE_PASSWORD_HASH!r}
sentinel, and is not {LEGACY_OPERATOR_USERNAME!r}.

Nothing has been written. This migration has rolled back.\
"""


def owner_of(eligible: Sequence[tuple[int, bool]]) -> int | None:
    """The one account that may own the tenant, or ``None`` if there is none.

    Ownership is not inferred from seniority. The account has to already carry
    the ``is_admin`` flag, because that flag is the only existing record of an
    administrative decision about this installation. Since
    :data:`ELIGIBLE_OPERATORS` sorts administrators first, the candidate is the
    first row -- and if that row is not an administrator, no eligible
    administrator exists at all.
    """
    if eligible and eligible[0][1]:
        return eligible[0][0]
    return None


def plan_memberships(
    eligible: Sequence[tuple[int, bool]],
) -> list[tuple[int, str]]:
    """Map eligible operators onto the roles they start with.

    Empty when no eligible administrator exists, rather than promoting
    somebody to fill the gap. A tenant with no owner is a state an explicit,
    audited grant can resolve later; a wrongly assigned owner is a privilege
    escalation nobody asked for.
    """
    if owner_of(eligible) is None:
        return []
    owner_id, _ = eligible[0]
    plan = [(owner_id, ROLE_OWNER)]
    for operator_id, is_admin in eligible[1:]:
        plan.append((operator_id, ROLE_ADMIN if is_admin else ROLE_OPERATOR))
    return plan


def require_owner(
    eligible: Sequence[tuple[int, bool]],
    data_exists: bool,
) -> None:
    """Refuse to create a tenant that would own data with nobody responsible.

    Kept separate from :func:`upgrade` so that the refusal is testable without
    an Alembic context, and so the condition is written down exactly once.
    """
    if data_exists and owner_of(eligible) is None:
        raise RuntimeError(NO_ELIGIBLE_OWNER)


def upgrade() -> None:
    bind = op.get_bind()

    # Read the answer to both questions before creating anything, so that the
    # refusal below cannot be preceded by a write. Postgres would roll the DDL
    # back anyway; this makes the guarantee visible instead of inferred.
    data_exists = bool(bind.execute(sa.text(TENANT_OWNABLE_DATA_EXISTS)).scalar())
    eligible = [
        (int(row[0]), bool(row[1]))
        for row in bind.execute(
            sa.text(ELIGIBLE_OPERATORS), ELIGIBILITY_PARAMS
        ).fetchall()
    ]

    require_owner(eligible, data_exists)

    op.create_table(
        "tenants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenants"),
    )
    # Unique index rather than a separate UNIQUE constraint plus index, which
    # is what the model declares and what ON CONFLICT (slug) above needs.
    op.create_index("ix_tenants_slug", "tenants", ["slug"], unique=True)

    op.create_table(
        "tenant_memberships",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("operator_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_memberships"),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name="fk_tenant_memberships_tenant",
            ondelete="CASCADE",
        ),
        # RESTRICT, for the same reason audit_logs.operator_id uses it: an
        # operator who has acted is deactivated, never deleted.
        sa.ForeignKeyConstraint(
            ["operator_id"],
            ["operators.id"],
            name="fk_tenant_memberships_operator",
            ondelete="RESTRICT",
        ),
        # One row per person per tenant. This is also the conflict target the
        # membership insert relies on.
        sa.UniqueConstraint("tenant_id", "operator_id", name="uq_tenant_membership"),
    )
    op.create_index(
        "ix_tenant_memberships_tenant_id", "tenant_memberships", ["tenant_id"]
    )
    # Indexed independently of the tenant: "which tenants may this operator
    # reach" runs on every authenticated request, keyed on the operator alone.
    op.create_index(
        "ix_tenant_memberships_operator_id", "tenant_memberships", ["operator_id"]
    )

    bind.execute(
        sa.text(INSERT_DEFAULT_TENANT),
        {"name": DEFAULT_TENANT_NAME, "slug": DEFAULT_TENANT_SLUG},
    )
    tenant_id = bind.execute(
        sa.text(SELECT_TENANT_BY_SLUG), {"slug": DEFAULT_TENANT_SLUG}
    ).scalar_one()

    for operator_id, role in plan_memberships(eligible):
        bind.execute(
            sa.text(INSERT_MEMBERSHIP),
            {"tenant_id": tenant_id, "operator_id": operator_id, "role": role},
        )


def downgrade() -> None:
    # Dropping these loses the tenant and its memberships, which is inherent
    # to reversing a migration whose whole content is those two tables. It is
    # not destructive of anything older: no pre-existing table gained a column
    # here, so there is no earlier state to reconstruct. Memberships first,
    # because the foreign key points that way.
    op.drop_table("tenant_memberships")
    op.drop_table("tenants")
