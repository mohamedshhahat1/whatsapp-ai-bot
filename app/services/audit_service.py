"""Recording what operators did.

One row per state-changing admin action. See :mod:`app.models.audit_log` for
what is and is not recorded, and migration 0010 for how the table is kept
append-only.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.services.auth_service import AuthService


class AuditService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._auth = AuthService(session)

    async def record(
        self,
        operator_id: int | None,
        action: str,
        *,
        resource_type: str,
        resource_id: str | int | None = None,
        details: dict[str, Any] | None = None,
        ip_address: str | None = None,
        commit: bool = True,
    ) -> AuditLog:
        """Record one administrative action.

        ``operator_id`` of None means the caller authenticated with the shared
        ADMIN_API_KEY, and the reserved legacy operator is resolved here. That
        lookup happens at most once per recorded action and never on the
        authentication path, which is why a shared-key request that changes
        nothing still issues no queries.

        ``resource_id`` is stringified because the column holds conversation
        ids, wa_ids and model names alike; see the model for why that beats
        three mostly-null columns.

        Pass ``commit=False`` when the caller owns the transaction. An audit
        row committed on its own can outlive an action that subsequently
        fails, and a log that records things which did not happen is worse
        than one with a gap in it.
        """
        resolved = operator_id
        if resolved is None:
            resolved = await self._auth.legacy_operator_id()
        entry = AuditLog(
            operator_id=resolved,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            details=details or {},
            ip_address=ip_address,
        )
        self._session.add(entry)
        if commit:
            await self._session.commit()
            await self._session.refresh(entry)
        return entry

    async def list_recent(self, limit: int = 50) -> list[AuditLog]:
        """Most recent actions first."""
        result = await self._session.execute(
            select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def list_for_resource(
        self, resource_type: str, resource_id: str | int, limit: int = 50
    ) -> list[AuditLog]:
        """What happened to one thing, most recent first."""
        result = await self._session.execute(
            select(AuditLog)
            .where(AuditLog.resource_type == resource_type)
            .where(AuditLog.resource_id == str(resource_id))
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
