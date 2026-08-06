"""Recording what operators did.

One row per state-changing admin action. See :mod:`app.models.audit_log` for
what is and is not recorded, and migration 0010 for how the table is kept
append-only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.services.auth_service import AuthService

# Rows per delete statement. audit_logs is written by every state-changing
# admin action, so a single unbounded DELETE would hold locks against all of
# them for its whole duration. A thousand at a time clears a year of history
# in a handful of round trips while never blocking an operator for long.
PURGE_BATCH_SIZE = 1000

# A ceiling on one sweep. The first run after enabling retention may face
# years of rows, and a task that runs until they are all gone is a task with
# no bound on its runtime. Whatever is left is taken by the next tick.
PURGE_MAX_BATCHES = 50

# Set for the duration of one transaction, and read by the trigger function
# installed in migration 0012. Anything that does not set it is refused, so
# the exemption cannot be reached by an ordinary request.
_ALLOW_PURGE = text("SELECT set_config('audit.allow_purge', 'on', true)")


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

    async def purge_older_than(
        self,
        cutoff: datetime,
        *,
        batch_size: int = PURGE_BATCH_SIZE,
        max_batches: int = PURGE_MAX_BATCHES,
    ) -> int:
        """Delete audit rows created before ``cutoff``. Returns rows removed.

        The only path in this codebase that removes audit history, and the
        only caller of the exemption added in migration 0012. Every batch
        opens a transaction, sets the transaction-local flag the trigger
        looks for, deletes, and commits -- so the permission to delete exists
        for the span of one statement and is gone before anything else runs.

        UPDATE is not affected and cannot be: no flag permits it. This
        expires records, it does not rewrite them, which is the part of
        append-only that matters.

        Stops at ``max_batches`` rather than running until the table is
        clear, so the first sweep after a long retention-free period cannot
        occupy a worker indefinitely. The remainder is taken by the next run,
        because the cutoff only moves forward.
        """
        removed = 0
        for _ in range(max_batches):
            await self._session.execute(_ALLOW_PURGE)
            result = await self._session.execute(
                select(AuditLog.id)
                .where(AuditLog.created_at < cutoff)
                .order_by(AuditLog.id)
                .limit(batch_size)
            )
            ids = list(result.scalars().all())
            if not ids:
                # Nothing to do. End the transaction the flag was set in
                # rather than leaving it open behind a return.
                await self._session.rollback()
                break
            await self._session.execute(delete(AuditLog).where(AuditLog.id.in_(ids)))
            await self._session.commit()
            removed += len(ids)
        return removed
