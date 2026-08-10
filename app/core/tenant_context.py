"""The authorised tenant a unit of work is acting inside.

Phase 1c. Phase 1b gave the business tables a NOT NULL ``tenant_id`` while
every read path stayed deployment-wide: a schema that knows who owns a row,
and an application that never asks. This type is how the application starts
asking.

It is passed explicitly -- a constructor argument, or a route dependency --
and never read from a context variable. An ambient ``contextvars`` tenant is
invisible at the call site, survives into background tasks that were never
meant to inherit it, and turns "is this query scoped?" into a question about
execution history rather than about the code in front of you. An argument
that must be supplied cannot be forgotten silently: either the call does not
type-check, or the repository raises.

How the tenant was decided is recorded alongside which one it is. D-6 requires
platform-level access to tenant-owned data to be auditable, and "which tenant"
is only half of that -- "and on what basis" is the half that makes an audit
row worth reading a year later.

There is deliberately no way to express "every tenant". A nullable tenant that
means unrestricted is the shape every cross-tenant leak in this codebase would
take, so the type refuses to hold one.
"""

from __future__ import annotations

from dataclasses import dataclass

# How a tenant context was arrived at.
#
# MEMBERSHIP   the operator belongs to exactly one tenant, so there was
#              nothing to choose.
# SELECTOR     the caller named a tenant explicitly and it was validated
#              against what that credential may reach.
# SOLE_TENANT  the deployment holds exactly one tenant, so the context is
#              objectively unambiguous. This is NOT the default-tenant
#              fallback: it stops being an answer the moment a second tenant
#              exists, where the fallback would happily keep returning one.
# SYSTEM       resolved outside any request, by a worker or the CLI.
SOURCE_MEMBERSHIP = "membership"
SOURCE_SELECTOR = "selector"
SOURCE_SOLE_TENANT = "sole_tenant"
SOURCE_SYSTEM = "system"


class MissingTenantContext(RuntimeError):
    """A tenant-scoped operation was reached without a tenant.

    Deliberately not an HTTPException. This is the repository and service
    layer's fail-closed guard, and those layers are also reached from Celery
    tasks and the CLI, where a 400 would be meaningless. Routes translate it;
    everything else should crash loudly rather than quietly widen a query.
    """


@dataclass(frozen=True)
class TenantContext:
    """The single tenant whose data this unit of work may touch.

    Frozen because a scope that can be reassigned mid-request is not a scope.
    A caller that needs a different tenant builds a different context, which
    is visible in a diff in a way that mutating a field is not.
    """

    tenant_id: int
    source: str
    via_legacy_key: bool = False

    def __post_init__(self) -> None:
        """Refuse a context that does not actually name a tenant.

        The type annotation says ``int`` and mypy gates ``app/``, so this only
        fires on a value that arrived untyped -- a query parameter, a task
        argument, a JSON frame. Those are exactly the paths where a None that
        silently meant "all tenants" would do the most damage.
        """
        if not isinstance(self.tenant_id, int) or self.tenant_id <= 0:
            raise MissingTenantContext(
                "A TenantContext must name a real tenant; got "
                f"{self.tenant_id!r}. There is no value here meaning "
                "'every tenant'."
            )

    @property
    def requires_platform_access_audit(self) -> bool:
        """True when a platform-level credential reached tenant-owned data.

        The shared ADMIN_API_KEY is held by whoever holds the environment
        file and belongs to no tenant. D-6 allows it to act inside one, and
        requires that doing so leaves a trace.
        """
        return self.via_legacy_key


def system_tenant_context(tenant_id: int) -> TenantContext:
    """A context for work with no request behind it.

    Workers and the CLI have no credential to resolve from, but they still
    have to say which tenant they are acting for rather than leaving it
    unstated. Named so that ``source`` distinguishes it in an audit row from
    anything a person did.
    """
    return TenantContext(tenant_id=tenant_id, source=SOURCE_SYSTEM)
