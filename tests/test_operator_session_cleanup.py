"""Expired operator sessions are swept away.

operator_sessions gained a row per login and, before this, never lost one.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.operator import Operator, OperatorSession
from app.services.auth_service import AuthService, hash_token

PASSWORD = "correct horse battery staple"


async def _purge_operator(session: AsyncSession, username: str) -> None:
    await session.execute(delete(Operator).where(Operator.username == username))
    await session.commit()


async def _add_session(
    db: AsyncSession,
    operator: Operator,
    token: str,
    expires_at: datetime,
) -> None:
    db.add(
        OperatorSession(
            operator_id=operator.id,
            token_hash=hash_token(token),
            expires_at=expires_at,
        )
    )
    await db.commit()


async def test_expired_sessions_go_and_live_ones_stay(
    db: AsyncSession, requires_database: None
) -> None:
    username = "sweep-op-" + uuid4().hex[:8]
    await _purge_operator(db, username)
    now = datetime.now(UTC)
    try:
        operator = await AuthService(db).create_operator(
            username, PASSWORD, "Sweep Operator"
        )
        await _add_session(db, operator, "dead", now - timedelta(minutes=1))
        await _add_session(db, operator, "live", now + timedelta(hours=1))

        deleted = await AuthService(db).purge_expired_sessions(now=now)

        assert deleted == 1
        remaining = await db.execute(
            select(OperatorSession.token_hash).where(
                OperatorSession.operator_id == operator.id
            )
        )
        assert remaining.scalars().all() == [hash_token("live")]
    finally:
        await _purge_operator(db, username)


async def test_a_revoked_but_unexpired_session_is_left_alone(
    db: AsyncSession, requires_database: None
) -> None:
    """Logging out is a fact worth keeping until the row expires anyway."""
    username = "sweep-op-" + uuid4().hex[:8]
    await _purge_operator(db, username)
    now = datetime.now(UTC)
    try:
        operator = await AuthService(db).create_operator(
            username, PASSWORD, "Sweep Operator"
        )
        await _add_session(db, operator, "revoked", now + timedelta(hours=1))
        assert await AuthService(db).revoke("revoked")

        assert await AuthService(db).purge_expired_sessions(now=now) == 0
    finally:
        await _purge_operator(db, username)


async def test_a_sweep_with_nothing_to_do_reports_zero(
    db: AsyncSession, requires_database: None
) -> None:
    """A moment before any session in the table could have been created."""
    long_ago = datetime.now(UTC) - timedelta(days=3650)
    assert await AuthService(db).purge_expired_sessions(now=long_ago) == 0
