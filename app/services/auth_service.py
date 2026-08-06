"""Operator authentication: password login and expiring bearer sessions."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.passwords import hash_password, verify_password
from app.models.operator import LEGACY_OPERATOR_USERNAME, Operator, OperatorSession

# How long a session lasts before the operator has to log in again. Twelve
# hours covers a working day without leaving a live credential on a phone
# overnight. A module constant rather than a setting: promoting it means
# editing a 130-field Settings class and .env.example, which is a reasonable
# follow-up but not something to do speculatively.
SESSION_TTL = timedelta(hours=12)

# 32 bytes through token_urlsafe, so ~43 characters of URL-safe text.
_TOKEN_BYTES = 32

# last_used_at exists to spot sessions nobody is using, so minute resolution
# is plenty. Writing it on every authenticated request would add a commit to
# each one to store a timestamp at a precision nothing reads.
_LAST_USED_RESOLUTION = timedelta(minutes=1)

# operator_sessions.user_agent is String(256).
_USER_AGENT_MAX = 256

# A real hash of a value nobody knows, computed once at import. Verifying an
# unknown username against this costs the same as verifying a known one, so
# response time does not reveal which usernames exist. One scrypt call at
# startup is the entire price.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(_TOKEN_BYTES))


class AuthenticationError(Exception):
    """Credentials were refused."""


def hash_token(token: str) -> str:
    """SHA-256 of a session token, hex encoded.

    Not a password KDF, and deliberately not one: a session token is 32 bytes
    of ``secrets`` output rather than something a person chose, so there is no
    low-entropy guess to slow down, and a per-request scrypt would make every
    authenticated call cost 16 MiB.
    """
    return hashlib.sha256(token.encode()).hexdigest()


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_username(self, username: str) -> Operator | None:
        result = await self._session.execute(
            select(Operator).where(Operator.username == username)
        )
        return result.scalar_one_or_none()

    async def create_operator(
        self,
        username: str,
        password: str,
        display_name: str,
        *,
        is_admin: bool = False,
    ) -> Operator:
        """Create an operator account with a hashed password."""
        operator = Operator(
            username=username,
            display_name=display_name,
            password_hash=hash_password(password),
            is_admin=is_admin,
        )
        self._session.add(operator)
        await self._session.commit()
        await self._session.refresh(operator)
        return operator

    async def login(
        self,
        username: str,
        password: str,
        *,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> tuple[Operator, str, datetime]:
        """Exchange a username and password for a session token.

        Raises :class:`AuthenticationError` for every failure -- unknown user,
        wrong password, deactivated account, the reserved legacy row -- with
        one message. Telling the caller which of those it was is telling an
        attacker which half of the guess to keep.
        """
        operator = await self.get_by_username(username)
        encoded = operator.password_hash if operator is not None else _DUMMY_HASH
        password_ok = verify_password(password, encoded)
        if operator is None or not password_ok or not operator.can_log_in:
            raise AuthenticationError("Invalid username or password")

        token = secrets.token_urlsafe(_TOKEN_BYTES)
        now = datetime.now(UTC)
        expires_at = now + SESSION_TTL
        self._session.add(
            OperatorSession(
                operator_id=operator.id,
                token_hash=hash_token(token),
                expires_at=expires_at,
                user_agent=user_agent[:_USER_AGENT_MAX] if user_agent else None,
                ip_address=ip_address,
            )
        )
        operator.last_login_at = now
        await self._session.commit()
        await self._session.refresh(operator)
        return operator, token, expires_at

    async def resolve(self, token: str) -> Operator | None:
        """The operator behind a session token, or None if it is not usable.

        None covers every reason equally: no such session, expired, revoked,
        or belonging to an account that has since been deactivated. The caller
        turns all of them into the same 401.
        """
        session = await self._session_for(token)
        if session is None or not session.is_valid():
            return None
        operator = await self._session.get(Operator, session.operator_id)
        if operator is None or not operator.is_active:
            return None
        now = datetime.now(UTC)
        stale = (
            session.last_used_at is None
            or now - session.last_used_at > _LAST_USED_RESOLUTION
        )
        if stale:
            session.last_used_at = now
            await self._session.commit()
        return operator

    async def revoke(self, token: str) -> bool:
        """End a session. True if this call is what ended it.

        The row is marked rather than deleted: a session ending is a fact with
        a time on it, and logging out is worth being able to see.
        """
        session = await self._session_for(token)
        if session is None or session.revoked_at is not None:
            return False
        session.revoked_at = datetime.now(UTC)
        await self._session.commit()
        return True

    async def legacy_operator_id(self) -> int:
        """The reserved row that shared-key requests are attributed to.

        Resolved on demand rather than at authentication time, so a request
        carrying only ADMIN_API_KEY still costs no query unless something
        actually needs to record who acted.
        """
        operator = await self.get_by_username(LEGACY_OPERATOR_USERNAME)
        if operator is None:  # pragma: no cover - seeded by migration 0010
            raise LookupError(
                "The reserved legacy-api-key operator is missing; "
                "run 'alembic upgrade head'"
            )
        return operator.id

    async def _session_for(self, token: str) -> OperatorSession | None:
        if not token:
            return None
        result = await self._session.execute(
            select(OperatorSession).where(
                OperatorSession.token_hash == hash_token(token)
            )
        )
        return result.scalar_one_or_none()
