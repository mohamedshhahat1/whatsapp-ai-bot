"""Tenants and the memberships that give operators reach into them.

A tenant is one business using this deployment. Operators stay global
identities -- one person, one login, one password -- and their authority
inside a tenant is a membership row that can be granted and revoked without
touching the account. That separation is what lets the same person work for
two tenants later without a second password, and it is why
``operator_sessions`` gains no tenant column: a session proves who you are,
not which business you are currently acting for.

Two identities deliberately hold no membership at all.

The reserved ``legacy-api-key`` operator stands for requests authenticated
with the shared ADMIN_API_KEY, which is a platform-level credential held by
whoever holds the environment file. Giving it a tenant role would convert
platform access into tenant access without anybody deciding to, which is the
boundary this model exists to draw.

Platform administration generally is a separate identity class. Reaching
tenant-owned data from it has to establish an explicit, audited tenant
context rather than inherit one from a row here.

No ORM relationships are declared. The sessions are async, and lazy loading
in an async context raises MissingGreenlet instead of emitting a query --
something this repository has already been bitten by once. Repositories join
explicitly instead.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"

# Ordered from most to least authority. A membership carries exactly one.
ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"

ROLES = (ROLE_OWNER, ROLE_ADMIN, ROLE_OPERATOR)

# The tenant migration 0015 creates for a deployment that predates tenancy.
DEFAULT_TENANT_SLUG = "default"


class Tenant(Base):
    """One business using this deployment."""

    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    # Stable, URL-safe and unique: the handle an onboarding flow can hand out
    # and a support engineer can quote, without exposing a row id.
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Suspension is how a tenant stops without its data being deleted, so this
    # is a state column rather than a boolean: "why is it off" is a question
    # somebody always ends up asking.
    status: Mapped[str] = mapped_column(
        String(16), default=STATUS_ACTIVE, server_default=text("'active'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TenantMembership(Base):
    """One operator's authority inside one tenant."""

    __tablename__ = "tenant_memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "operator_id", name="uq_tenant_membership"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # RESTRICT, matching audit_logs.operator_id. An operator who has acted is
    # deactivated rather than deleted, so removing somebody from a tenant has
    # to be revoking this row -- never deleting the account, which would take
    # their audit trail with it.
    operator_id: Mapped[int] = mapped_column(
        ForeignKey("operators.id", ondelete="RESTRICT"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
