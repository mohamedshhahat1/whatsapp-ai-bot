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
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import DEFAULT_TENANT_SLUG, Tenant


async def default_tenant_id(session: AsyncSession) -> int:
    """The tenant that owns data written without an explicit tenant context.

    One lookup on a unique slug. Deliberately not cached: the value belongs to
    a database rather than to a process, so a module-level cache would outlive
    the thing it was true for -- and Phase 1c deletes these call sites rather
    than optimising them.
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
