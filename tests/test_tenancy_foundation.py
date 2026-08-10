"""Migration 0015: the tenancy foundation, and the owner it refuses to invent.

The interesting part of 0015 is not its two tables, it is the decision it
makes about who owns the tenant that existing data becomes attached to. Four
things are pinned here:

* the reserved shared-key operator is never eligible to own a tenant,
* only an account already flagged ``is_admin`` may become owner,
* a populated database with no such account aborts the migration,
* an empty database does not, because there is nothing to own yet.

The last looks like a loophole and is not. CI migrates an empty database from
nothing on every run and so does a restore from backup; refusing there would
make the migration chain unrunnable while protecting no data.

The database tests run the migration's own SQL, taken from the module, rather
than restating it -- a test that restated the query would keep passing after
somebody changed the real one. Each opens a transaction and rolls it back, so
the schema CI migrated is unchanged afterwards whether the assertions pass or
not.
"""

import importlib.util
import inspect
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.passwords import UNUSABLE_PASSWORD_HASH
from app.models.operator import LEGACY_OPERATOR_USERNAME
from app.models.tenant import (
    DEFAULT_TENANT_SLUG,
    ROLE_ADMIN,
    ROLE_OPERATOR,
    ROLE_OWNER,
)

TENANCY_FOUNDATION = "0015_tenancy_foundation"
ANALYTICS_ROLLUP = "0014_analytics_daily_rollup"

# A hash that is merely not the sentinel. Eligibility asks whether an account
# could ever authenticate at all, not whether this particular string verifies,
# so paying for a real scrypt derivation here would buy nothing.
NOT_THE_SENTINEL = "scrypt$16384$8$1$00$00"

_INSERT_OPERATOR = """
INSERT INTO operators
    (username, display_name, password_hash, is_active, is_admin)
VALUES (:username, 'Tenancy Test', :password_hash, true, :is_admin)
RETURNING id
"""

_DEACTIVATE_EVERYONE_ELSE = """
UPDATE operators SET is_active = false WHERE username <> :username
"""

# Since 0016 a customer row names its owner, so the tenant is passed in rather
# than defaulted. Which tenant does not matter to 0015 -- it asks whether any
# ownable row exists at all -- so each test hands over one it knows about.
_INSERT_CUSTOMER = """
INSERT INTO users (tenant_id, channel, external_id, wa_id, name)
VALUES (:tenant_id, 'whatsapp', :external_id, :external_id, 'Tenancy test')
"""

_DELETE_MEMBERSHIPS = "DELETE FROM tenant_memberships"

# Reaching the pre-0015 state by moving the slug aside instead of deleting the
# row. Every tenant reference added in 0016 is ON DELETE RESTRICT, so a delete
# would either fail against rows this test does not own or, worse, require
# destroying them. 0015 resolves the default tenant by slug and conflicts on
# slug, so a rename produces precisely the precondition it cares about -- and
# unlike a delete it does so whatever else the database happens to contain.
_RENAME_TENANT = "UPDATE tenants SET slug = :slug WHERE id = :tenant_id"

_TABLE_EXISTS = "SELECT to_regclass(:name) IS NOT NULL"

_MEMBERSHIP_CONSTRAINT = """
SELECT count(*) FROM pg_constraint WHERE conname = 'uq_tenant_membership'
"""

_OPERATOR_ID = "SELECT id FROM operators WHERE username = :username"

_MEMBERSHIPS_FOR_USERNAME = """
SELECT count(*)
FROM tenant_memberships m
JOIN operators o ON o.id = m.operator_id
WHERE o.username = :username
"""

_MEMBERSHIP_ROLE = """
SELECT role FROM tenant_memberships
WHERE tenant_id = :tenant_id AND operator_id = :operator_id
"""

_MEMBERSHIP_COUNT = """
SELECT count(*) FROM tenant_memberships WHERE tenant_id = :tenant_id
"""


def _migration(revision: str) -> ModuleType:
    """Load a migration module by file name, without an Alembic context.

    ``alembic/versions`` is not an importable package, so this goes through
    importlib. Worth the trouble because the tests below then execute the
    migration's real SQL.
    """
    path = Path("alembic") / "versions" / f"{revision}.py"
    spec = importlib.util.spec_from_file_location(revision, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _foundation() -> ModuleType:
    return _migration(TENANCY_FOUNDATION)


# --- The constants the migration copied rather than imported ----------------
#
# The copies are deliberate: a migration must not change meaning when an
# application constant is edited years later. These tests are what stops the
# copies from silently drifting instead.


def test_the_copied_sentinel_hash_still_matches_the_application() -> None:
    assert _foundation().UNUSABLE_PASSWORD_HASH == UNUSABLE_PASSWORD_HASH


def test_the_copied_legacy_username_still_matches_the_application() -> None:
    assert _foundation().LEGACY_OPERATOR_USERNAME == LEGACY_OPERATOR_USERNAME


def test_the_copied_role_names_still_match_the_model() -> None:
    module = _foundation()
    assert (module.ROLE_OWNER, module.ROLE_ADMIN, module.ROLE_OPERATOR) == (
        ROLE_OWNER,
        ROLE_ADMIN,
        ROLE_OPERATOR,
    )


def test_the_copied_default_slug_still_matches_the_model() -> None:
    assert _foundation().DEFAULT_TENANT_SLUG == DEFAULT_TENANT_SLUG


# --- Where it sits in the chain ---------------------------------------------


def test_the_foundation_follows_the_analytics_rollup() -> None:
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    revision = scripts.get_revision(TENANCY_FOUNDATION)
    assert revision.down_revision == ANALYTICS_ROLLUP


def test_the_chain_still_has_a_single_head() -> None:
    """Two heads would make `alembic upgrade head` ambiguous and fail."""
    assert len(ScriptDirectory.from_config(Config("alembic.ini")).get_heads()) == 1


def test_the_revision_identifier_fits_the_version_column() -> None:
    """alembic_version.version_num is VARCHAR(32).

    A longer identifier passes every DDL step and then fails on the final
    stamp, after the whole migration has run. It has happened here before.
    """
    assert len(_foundation().revision) <= 32


# --- Who may own a tenant ---------------------------------------------------


def test_the_owner_candidate_must_be_an_administrator() -> None:
    """``is_admin`` is the only existing record of an administrative decision.

    Without it there is no evidence that anybody chose this account, and
    picking one anyway would be inventing an owner.
    """
    module = _foundation()
    assert module.owner_of([(7, True), (3, True)]) == 7
    assert module.owner_of([(4, False), (6, False)]) is None
    assert module.owner_of([]) is None


def test_a_populated_database_with_no_eligible_operator_is_refused() -> None:
    module = _foundation()
    with pytest.raises(RuntimeError) as raised:
        module.require_owner([], data_exists=True)
    assert module.NO_ELIGIBLE_OWNER in str(raised.value)


def test_operators_without_the_admin_flag_are_refused_when_data_exists() -> None:
    """Having accounts is not the same as having an owner."""
    module = _foundation()
    with pytest.raises(RuntimeError):
        module.require_owner([(4, False), (6, False)], data_exists=True)


def test_the_refusal_tells_the_deployment_operator_what_to_do() -> None:
    """An abort nobody can act on is just an outage."""
    message = _foundation().NO_ELIGIBLE_OWNER
    assert "python -m app.cli create-admin" in message
    assert LEGACY_OPERATOR_USERNAME in message
    assert "rolled back" in message


def test_an_empty_database_is_not_refused() -> None:
    """The documented exception, pinned so it cannot be tightened by accident.

    Nothing exists to be owned, so no ambiguous ownership can be created. This
    is the path CI and a disaster-recovery restore both take.
    """
    module = _foundation()
    module.require_owner([], data_exists=False)
    module.require_owner([(4, False)], data_exists=False)


def test_an_eligible_administrator_satisfies_the_guard() -> None:
    _foundation().require_owner([(1, True)], data_exists=True)


# --- Who gets which role ----------------------------------------------------


def test_the_first_eligible_administrator_becomes_the_owner() -> None:
    plan = _foundation().plan_memberships([(7, True), (3, True), (9, False)])
    assert plan == [(7, ROLE_OWNER), (3, ROLE_ADMIN), (9, ROLE_OPERATOR)]


def test_a_deployment_with_no_administrator_gets_no_owner() -> None:
    """No owner is better than the wrong owner.

    A tenant with no owner is recoverable by an explicit, audited grant. An
    owner promoted from whoever happened to have the lowest id is a privilege
    escalation nobody asked for, and one nobody would notice.
    """
    assert _foundation().plan_memberships([(4, False), (6, False)]) == []


def test_no_eligible_operators_produces_no_memberships() -> None:
    assert _foundation().plan_memberships([]) == []


def test_the_upgrade_refuses_before_it_writes_anything() -> None:
    """Order is the guarantee in the error message.

    'Nothing has been written' has to be true of the code, not only of the
    surrounding transaction.
    """
    source = inspect.getsource(_foundation().upgrade)
    assert source.index("require_owner") < source.index("create_table")
    assert source.index("require_owner") < source.index("INSERT_DEFAULT_TENANT")


# --- What landed in the database --------------------------------------------


async def test_the_tenancy_tables_exist_after_upgrade(db: AsyncSession) -> None:
    for table in ("tenants", "tenant_memberships"):
        exists = await db.scalar(text(_TABLE_EXISTS), {"name": table})
        assert exists, f"table {table} missing after alembic upgrade head"


async def test_one_membership_per_person_per_tenant_is_enforced(
    db: AsyncSession,
) -> None:
    assert await db.scalar(text(_MEMBERSHIP_CONSTRAINT)) == 1


async def test_the_shared_key_operator_holds_no_membership(db: AsyncSession) -> None:
    """The decision itself, asserted against the database CI migrated."""
    count = await db.scalar(
        text(_MEMBERSHIPS_FOR_USERNAME),
        {"username": LEGACY_OPERATOR_USERNAME},
    )
    assert count == 0


async def test_the_eligibility_query_never_returns_the_shared_key_operator(
    db: AsyncSession,
) -> None:
    module = _foundation()
    legacy_id = await db.scalar(
        text(_OPERATOR_ID),
        {"username": LEGACY_OPERATOR_USERNAME},
    )
    assert legacy_id is not None, "migration 0010 seeds this row"

    result = await db.execute(
        text(module.ELIGIBLE_OPERATORS),
        module.ELIGIBILITY_PARAMS,
    )
    assert legacy_id not in [row[0] for row in result.fetchall()]


# --- The two production cases, run for real ---------------------------------


async def test_an_existing_real_operator_becomes_the_default_tenant_owner(
    db: AsyncSession,
) -> None:
    """The normal upgrade of a deployment that has been running for a while.

    Everybody except the new account is deactivated inside the transaction, so
    'the first eligible row' is deterministic whatever else the test database
    happens to contain.

    The pre-0015 state -- nothing answers to the default slug -- is produced by
    renaming that tenant rather than deleting it, for the reason recorded above
    _RENAME_TENANT. Whatever rows this database already holds keep the owner
    they have; only the slug moves, so the migration below has to create a
    tenant of its own, which the last assertion checks it did.
    """
    module = _foundation()
    username = "tenancy-owner-" + uuid4().hex[:8]
    try:
        operator_id = await db.scalar(
            text(_INSERT_OPERATOR),
            {
                "username": username,
                "password_hash": NOT_THE_SENTINEL,
                "is_admin": True,
            },
        )
        await db.execute(text(_DEACTIVATE_EVERYONE_ELSE), {"username": username})
        await db.execute(text(_DELETE_MEMBERSHIPS))

        original_id = await db.scalar(
            text(module.SELECT_TENANT_BY_SLUG),
            {"slug": DEFAULT_TENANT_SLUG},
        )
        assert original_id is not None, "migration 0015 seeds this row"
        await db.execute(
            text(_RENAME_TENANT),
            {"slug": "pre-0015-" + uuid4().hex[:12], "tenant_id": original_id},
        )

        await db.execute(
            text(_INSERT_CUSTOMER),
            {
                "tenant_id": original_id,
                "external_id": "tenancy-" + uuid4().hex[:12],
            },
        )

        data_exists = bool(await db.scalar(text(module.TENANT_OWNABLE_DATA_EXISTS)))
        assert data_exists

        result = await db.execute(
            text(module.ELIGIBLE_OPERATORS),
            module.ELIGIBILITY_PARAMS,
        )
        eligible = [(int(row[0]), bool(row[1])) for row in result.fetchall()]
        assert eligible == [(operator_id, True)]

        module.require_owner(eligible, data_exists)

        await db.execute(
            text(module.INSERT_DEFAULT_TENANT),
            {"name": module.DEFAULT_TENANT_NAME, "slug": DEFAULT_TENANT_SLUG},
        )
        tenant_id = await db.scalar(
            text(module.SELECT_TENANT_BY_SLUG),
            {"slug": DEFAULT_TENANT_SLUG},
        )
        assert tenant_id != original_id, "the migration reused an existing tenant"
        for member_id, role in module.plan_memberships(eligible):
            await db.execute(
                text(module.INSERT_MEMBERSHIP),
                {
                    "tenant_id": tenant_id,
                    "operator_id": member_id,
                    "role": role,
                },
            )

        role = await db.scalar(
            text(_MEMBERSHIP_ROLE),
            {"tenant_id": tenant_id, "operator_id": operator_id},
        )
        assert role == ROLE_OWNER

        # And nobody else was admitted -- in particular not the shared key.
        total = await db.scalar(text(_MEMBERSHIP_COUNT), {"tenant_id": tenant_id})
        assert total == 1
    finally:
        await db.rollback()


async def test_a_deployment_with_only_the_shared_key_is_refused_for_real(
    db: AsyncSession,
) -> None:
    """The case a deployment operator must never pass through silently.

    Every real account is deactivated inside the transaction, leaving the
    reserved shared-key row as the only administrative identity -- exactly the
    state of a deployment that has never created an administrator.
    """
    module = _foundation()
    try:
        await db.execute(
            text(_DEACTIVATE_EVERYONE_ELSE),
            {"username": LEGACY_OPERATOR_USERNAME},
        )
        tenant_id = await db.scalar(
            text(module.SELECT_TENANT_BY_SLUG),
            {"slug": DEFAULT_TENANT_SLUG},
        )
        assert tenant_id is not None, "migration 0015 seeds this row"
        await db.execute(
            text(_INSERT_CUSTOMER),
            {
                "tenant_id": tenant_id,
                "external_id": "tenancy-" + uuid4().hex[:12],
            },
        )

        data_exists = bool(await db.scalar(text(module.TENANT_OWNABLE_DATA_EXISTS)))
        assert data_exists, "the customer row above makes ownership a real question"

        result = await db.execute(
            text(module.ELIGIBLE_OPERATORS),
            module.ELIGIBILITY_PARAMS,
        )
        assert result.fetchall() == []

        with pytest.raises(RuntimeError):
            module.require_owner([], data_exists)
    finally:
        await db.rollback()
