"""Resolving the tenant a writer is acting for.

Phase 1b gives the tenant-owned tables a NOT NULL ``tenant_id`` before any
request carries a tenant context, so every writer needs an answer to "whose
row is this" today.

Two kinds of answer exist, and the difference matters.

Most writers derive it from the row they are attaching to -- a message from
its conversation, a conversation from its customer -- inside the same INSERT.
That is not a shortcut: it makes a cross-tenant pairing impossible to write
rather than merely discouraged, and it keeps those statements single and
atomic, which is what the reserve-before-send guarantee is built on.

The rest have no parent to derive from: a customer arriving from a webhook, a
knowledge-base document, a model call logged without a conversation, a
nightly rollup. Until Phase 1c threads an authenticated tenant through the
call, those fall back to the tenant 0015 created for a deployment that
predates tenancy. The fallback is a keyword argument every caller can
override, so the tests that need two tenants have a way in, and Phase 1c has
a seam to thread real context through instead of a rewrite.

It fails loudly when the tenant is missing. Inventing one would be how a row
ends up in the wrong business's dashboard.

Phase 1c adds the questions a request has to answer before it may touch
anything: which tenants exist, which of them own data, and which of them a
given operator may act inside. They live here, next to the writers, so that a
request, a worker and the CLI all get the same answers from the same
statements rather than three similar ones.
"""

from __future__ import annotations

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import DEFAULT_TENANT_SLUG, Tenant, TenantMembership

# Every tenant that owns a row anywhere, including the audit trail.
#
# Copied verbatim from migration 0016, which uses it to decide whether a
# downgrade can still represent the data. tests/test_tenant_context.py holds
# the two texts equal, because the migration must not import application code
# -- its own docstring explains why -- and the alternative to a checked copy
# is two definitions of "a tenant that owns data" drifting apart while the
# downgrade guard, the restore drill and push suppression each believe a
# different one.
#
# UNION rather than UNION ALL: the question is how many distinct tenants there
# are, not how many rows they hold.
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


async def default_tenant_id(session: AsyncSession) -> int:
    """The tenant that owns data written without an explicit tenant context.

    One lookup on a unique slug. Deliberately not cached: the value belongs to
    a database rather than to a process, so a module-level cache would outlive
    the thing it was true for.

    Phase 1c does not call this from any request path. It remains for the
    writers 1b left defaulting -- the inbound webhook, whose real per-tenant
    resolution is Phase 3 -- and for tests. Authenticated tenant-scoped work
    resolves through the functions below instead, which is the whole point of
    D-6: a fallback that always answers cannot fail closed.
    """
    tenant_id = await session.scalar(
        select(Tenant.id).where(Tenant.slug == DEFAULT_TENANT_SLUG)
    )
    if tenant_id is None:
        raise RuntimeError(
            "No default tenant exists. Migration 0015 creates it; a deployment "
            "that removed it must pass tenant_id explicitly."
        )
    return int(tenant_id)


async def resolve_tenant_id(session: AsyncSession, tenant_id: int | None) -> int:
    """Honour an explicit tenant, else fall back to the default one."""
    if tenant_id is not None:
        return tenant_id
    return await default_tenant_id(session)


async def count_tenants(session: AsyncSession) -> int:
    """How many tenants this deployment holds."""
    return int(await session.scalar(select(func.count()).select_from(Tenant)) or 0)


async def sole_tenant_id(session: AsyncSession) -> int | None:
    """The only tenant, when there is exactly one. None otherwise.

    ``LIMIT 2`` rather than a count: the question is only ever "is there
    exactly one", and on a deployment with many tenants counting them all to
    learn that the answer is no would be work done for nothing.

    None is returned for both zero and many, which are different failures --
    the caller distinguishes them, because "nothing to act on" is a 403 and
    "say which one" is a 400.
    """
    rows = (await session.scalars(select(Tenant.id).order_by(Tenant.id).limit(2))).all()
    return int(rows[0]) if len(rows) == 1 else None


async def tenant_exists(session: AsyncSession, tenant_id: int) -> bool:
    """Whether a tenant id names a real tenant."""
    found = await session.scalar(select(Tenant.id).where(Tenant.id == tenant_id))
    return found is not None


async def tenant_ids_for_operator(session: AsyncSession, operator_id: int) -> list[int]:
    """Every tenant this operator holds a membership in, lowest id first.

    Membership answers "which tenant", and nothing else. ``role`` is read by
    nobody here on purpose: what an operator may *do* inside a tenant is
    Phase 2, and consulting the column now would be that phase arriving
    early under a different name.

    No join to ``tenants``. The foreign key is ON DELETE CASCADE, so a
    membership whose tenant is gone is gone with it, and a join asserting
    otherwise would be a predicate that can never be false.

    An empty list means no tenant is reachable. It never means all of them.
    """
    rows = await session.scalars(
        select(TenantMembership.tenant_id)
        .where(TenantMembership.operator_id == operator_id)
        .order_by(TenantMembership.tenant_id)
    )
    return [int(row) for row in rows]


async def tenant_ids_owning_data(session: AsyncSession) -> list[int]:
    """Every tenant that owns at least one row, lowest id first.

    Ownership rather than existence. A tenant row created by an onboarding
    flow that never went anywhere owns nothing, and treating it as live would
    make the deployment behave as multi-tenant before it is.
    """
    rows = await session.execute(text(TENANT_IDS_IN_USE))
    return sorted(int(row[0]) for row in rows)


async def more_than_one_tenant_owns_data(session: AsyncSession) -> bool:
    """Whether this deployment is genuinely serving multiple tenants.

    The condition D-2 suspends push notifications on: ``device_tokens`` has no
    owner column until Phase 6, so a second data-owning tenant is the point
    at which fanning out to every registered device stops being correct.
    """
    return len(await tenant_ids_owning_data(session)) > 1
