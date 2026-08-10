"""The tenant context seam: what it resolves, and what it refuses.

The refusals carry most of the weight here. A resolver that returns the right
tenant for a well-formed request is easy; the isolation guarantee lives in
what happens to a request that names somebody else's tenant, names none on a
multi-tenant deployment, or arrives from a credential with no tenant at all.
"""

from __future__ import annotations

import importlib.util
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.passwords import UNUSABLE_PASSWORD_HASH
from app.core.tenant_context import (
    SOURCE_MEMBERSHIP,
    SOURCE_SELECTOR,
    SOURCE_SOLE_TENANT,
    SOURCE_SYSTEM,
    MissingTenantContext,
    TenantContext,
    system_tenant_context,
)
from app.dependencies.deps import Principal, require_tenant_context
from app.models.operator import LEGACY_OPERATOR_USERNAME, Operator
from app.models.tenant import ROLE_OPERATOR, TenantMembership
from app.repositories.tenant import (
    TENANT_IDS_IN_USE,
    count_tenants,
    more_than_one_tenant_owns_data,
    sole_tenant_id,
    tenant_exists,
    tenant_ids_for_operator,
    tenant_ids_owning_data,
)
from tests.conftest import Customer, create_customer, new_wa_id, purge

MIGRATION_0016 = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "0016_tenant_ownership.py"
)

LEGACY = Principal(
    operator_id=None,
    username=LEGACY_OPERATOR_USERNAME,
    is_admin=True,
    via_legacy_key=True,
)


def _load_migration() -> ModuleType:
    """Import 0016 by path; its filename is not a valid module name."""
    spec = importlib.util.spec_from_file_location("migration_0016", MIGRATION_0016)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalised(sql: str) -> str:
    """Collapse whitespace so formatting differences do not fail the check."""
    return " ".join(sql.split())


async def _operator_context(
    db: AsyncSession, operator_id: int, **selector: str
) -> TenantContext:
    """Resolve as a bearer-session operator."""
    principal = Principal(
        operator_id=operator_id,
        username=f"operator-{operator_id}",
        is_admin=False,
        via_legacy_key=False,
    )
    return await require_tenant_context(
        principal=principal,
        db=db,
        x_tenant_id=selector.get("x_tenant_id", ""),
        tenant_id=selector.get("tenant_id", ""),
    )


async def _legacy_context(db: AsyncSession, **selector: str) -> TenantContext:
    """Resolve as the shared ADMIN_API_KEY."""
    return await require_tenant_context(
        principal=LEGACY,
        db=db,
        x_tenant_id=selector.get("x_tenant_id", ""),
        tenant_id=selector.get("tenant_id", ""),
    )


@pytest.fixture
async def operator(db: AsyncSession) -> AsyncIterator[int]:
    """A real operator row with no membership yet.

    Torn down explicitly: operators.id is referenced by tenant_memberships
    with ON DELETE RESTRICT, so the memberships have to go first. Deleting
    them here rather than relying on the tenant cascade keeps this fixture
    correct whichever order it and ``other_tenant`` unwind in.
    """
    created = Operator(
        username="phase1c-" + uuid4().hex[:12],
        display_name="Phase 1c Test Operator",
        password_hash=UNUSABLE_PASSWORD_HASH,
    )
    db.add(created)
    await db.flush()
    operator_id = int(created.id)
    await db.commit()
    try:
        yield operator_id
    finally:
        await db.execute(
            text("DELETE FROM tenant_memberships WHERE operator_id = :id"),
            {"id": operator_id},
        )
        await db.execute(
            text("DELETE FROM operators WHERE id = :id"), {"id": operator_id}
        )
        await db.commit()


async def _grant(db: AsyncSession, tenant_id: int, operator_id: int) -> None:
    db.add(
        TenantMembership(
            tenant_id=tenant_id, operator_id=operator_id, role=ROLE_OPERATOR
        )
    )
    await db.commit()


# -- the definition of a live tenant ----------------------------------------


def test_the_application_and_the_migration_agree_on_data_ownership() -> None:
    """One definition of "a tenant that owns data", not two.

    0016's downgrade refuses when this query returns more than one row, the
    restore drill leans on the same schema, and D-2 suspends push
    notifications on the same condition. Two copies that drifted would mean
    the database and the application disagreeing about whether a deployment
    is multi-tenant -- and the application's copy is the one that decides
    whether a customer's name reaches another business's phone.
    """
    migration = _load_migration()
    assert _normalised(TENANT_IDS_IN_USE) == _normalised(migration.TENANT_IDS_IN_USE)


# -- the type itself ---------------------------------------------------------


def test_a_context_cannot_be_built_without_a_real_tenant() -> None:
    """None never means "every tenant", so it is not constructible."""
    for absent in (None, 0, -1):
        with pytest.raises(MissingTenantContext):
            TenantContext(absent, SOURCE_SELECTOR)  # type: ignore[arg-type]


def test_a_context_records_how_it_was_resolved() -> None:
    """The audit trail needs the basis, not only the answer."""
    assert system_tenant_context(7).source == SOURCE_SYSTEM
    assert system_tenant_context(7).tenant_id == 7


def test_only_the_shared_key_is_flagged_for_platform_access_audit() -> None:
    """D-6 marks platform-level reach into a tenant; membership is not that."""
    platform = TenantContext(1, SOURCE_SOLE_TENANT, via_legacy_key=True)
    member = TenantContext(1, SOURCE_MEMBERSHIP)
    assert platform.requires_platform_access_audit is True
    assert member.requires_platform_access_audit is False


# -- resolving for an operator ----------------------------------------------


async def test_an_operator_with_one_membership_needs_no_selector(
    db: AsyncSession, default_tenant: int, operator: int
) -> None:
    await _grant(db, default_tenant, operator)
    context = await _operator_context(db, operator)
    assert context.tenant_id == default_tenant
    assert context.source == SOURCE_MEMBERSHIP
    assert context.via_legacy_key is False


async def test_an_operator_with_no_membership_reaches_nothing(
    db: AsyncSession, default_tenant: int, operator: int
) -> None:
    """403, and emphatically not the default tenant.

    This is the case a fallback would have quietly resolved: an account that
    belongs to no business would have been handed the deployment's original
    one, which holds the real customers.
    """
    with pytest.raises(HTTPException) as refused:
        await _operator_context(db, operator)
    assert refused.value.status_code == 403


async def test_an_operator_in_two_tenants_must_say_which(
    db: AsyncSession, default_tenant: int, other_tenant: int, operator: int
) -> None:
    await _grant(db, default_tenant, operator)
    await _grant(db, other_tenant, operator)

    with pytest.raises(HTTPException) as refused:
        await _operator_context(db, operator)
    assert refused.value.status_code == 400

    chosen = await _operator_context(db, operator, x_tenant_id=str(other_tenant))
    assert chosen.tenant_id == other_tenant
    assert chosen.source == SOURCE_SELECTOR


async def test_an_operator_cannot_select_a_tenant_they_do_not_belong_to(
    db: AsyncSession, default_tenant: int, other_tenant: int, operator: int
) -> None:
    """404 rather than 403: the answer must not confirm the tenant exists.

    ``other_tenant`` is real and the operator is not in it. A 403 would say
    so, which is the difference between refusing a request and handing over a
    list of tenant ids to try next.
    """
    await _grant(db, default_tenant, operator)
    with pytest.raises(HTTPException) as refused:
        await _operator_context(db, operator, x_tenant_id=str(other_tenant))
    assert refused.value.status_code == 404

    absent = 2_000_000_000
    with pytest.raises(HTTPException) as missing:
        await _operator_context(db, operator, x_tenant_id=str(absent))
    assert missing.value.status_code == 404
    assert refused.value.detail == missing.value.detail


# -- resolving for the shared key -------------------------------------------


async def test_the_shared_key_binds_to_the_only_tenant_there_is(
    db: AsyncSession, default_tenant: int
) -> None:
    """The existing mobile client keeps working, without a default lookup.

    One tenant is not a preference, it is the only possible answer, so
    binding to it decides nothing on the caller's behalf.
    """
    held = await count_tenants(db)
    assert held == 1, (
        "This test describes a single-tenant deployment; the database holds "
        f"{held} tenants, so an earlier test leaked one."
    )
    context = await _legacy_context(db)
    assert context.tenant_id == default_tenant
    assert context.source == SOURCE_SOLE_TENANT
    assert context.requires_platform_access_audit is True


async def test_the_shared_key_must_choose_once_a_second_tenant_exists(
    db: AsyncSession, default_tenant: int, other_tenant: int
) -> None:
    """Where DEFAULT_TENANT_SLUG would still have answered, this refuses."""
    with pytest.raises(HTTPException) as refused:
        await _legacy_context(db)
    assert refused.value.status_code == 400

    chosen = await _legacy_context(db, tenant_id=str(other_tenant))
    assert chosen.tenant_id == other_tenant
    assert chosen.source == SOURCE_SELECTOR
    assert chosen.requires_platform_access_audit is True


async def test_the_shared_keys_selector_is_validated(
    db: AsyncSession, default_tenant: int, other_tenant: int
) -> None:
    with pytest.raises(HTTPException) as refused:
        await _legacy_context(db, tenant_id="2000000000")
    assert refused.value.status_code == 404


# -- selector parsing --------------------------------------------------------


async def test_two_selectors_that_disagree_are_refused(
    db: AsyncSession, default_tenant: int, other_tenant: int
) -> None:
    """No precedence rule. A client sending both with different values has a
    bug, and picking a winner would settle an authorisation boundary by
    convention rather than by intent.
    """
    with pytest.raises(HTTPException) as refused:
        await _legacy_context(
            db, x_tenant_id=str(default_tenant), tenant_id=str(other_tenant)
        )
    assert refused.value.status_code == 400

    agreeing = await _legacy_context(
        db, x_tenant_id=str(other_tenant), tenant_id=str(other_tenant)
    )
    assert agreeing.tenant_id == other_tenant


async def test_a_selector_that_is_not_a_tenant_id_is_refused(
    db: AsyncSession, default_tenant: int
) -> None:
    for nonsense in ("abc", "1.5", "-3", "0"):
        with pytest.raises(HTTPException) as refused:
            await _legacy_context(db, x_tenant_id=nonsense)
        assert refused.value.status_code == 400


# -- the ownership helpers ---------------------------------------------------


async def test_tenant_lookups_answer_from_the_database(
    db: AsyncSession, default_tenant: int, other_tenant: int, operator: int
) -> None:
    assert await tenant_exists(db, default_tenant) is True
    assert await tenant_exists(db, 2_000_000_000) is False
    # Two tenants exist, so there is no sole tenant to name.
    assert await sole_tenant_id(db) is None
    assert await tenant_ids_for_operator(db, operator) == []
    await _grant(db, other_tenant, operator)
    assert await tenant_ids_for_operator(db, operator) == [other_tenant]


async def test_owning_data_is_not_the_same_as_existing(
    db: AsyncSession, default_tenant: int, other_tenant: int
) -> None:
    """A tenant row with nothing under it does not make a deployment
    multi-tenant.

    D-2 hangs on this distinction: an onboarding flow that created a tenant
    and stopped must not suspend push notifications for the business that is
    actually using the system.
    """
    before = await tenant_ids_owning_data(db)
    assert other_tenant not in before

    wa_id = new_wa_id()
    created: Customer | None = None
    try:
        created = await create_customer(db, wa_id, tenant_id=other_tenant)
        assert created.user_id
        owning = await tenant_ids_owning_data(db)
        assert other_tenant in owning
        assert await more_than_one_tenant_owns_data(db) is True
    finally:
        await purge(db, wa_id)

    assert other_tenant not in await tenant_ids_owning_data(db)
