"""Operator authentication.

Split the way conftest requires: service behaviour through the ``db``
fixture in async tests, the endpoint contract through TestClient in
synchronous ones. The two must not meet in a single test -- TestClient runs
the app in its own event loop on another thread, and asyncpg connections
belong to the loop that opened them.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.operator import LEGACY_OPERATOR_USERNAME, Operator, OperatorSession
from app.services.auth_service import AuthService, hash_token

PASSWORD = "correct horse battery staple"


async def _purge_operator(session: AsyncSession, username: str) -> None:
    """Remove a throwaway operator and its sessions.

    Sessions first: the FK is ON DELETE CASCADE, but doing it explicitly keeps
    this working the same way as conftest's ``purge``.
    """
    params = {"username": username}
    await session.execute(
        text(
            "DELETE FROM operator_sessions WHERE operator_id IN "
            "(SELECT id FROM operators WHERE username = :username)"
        ),
        params,
    )
    await session.execute(
        text("DELETE FROM operators WHERE username = :username"), params
    )
    await session.commit()


@pytest.fixture
async def operator_account(db: AsyncSession) -> AsyncIterator[Operator]:
    """A throwaway operator with a known password."""
    username = "test-op-" + uuid4().hex[:8]
    created = await AuthService(db).create_operator(
        username, PASSWORD, "Test Operator"
    )
    try:
        yield created
    finally:
        await _purge_operator(db, username)


async def test_login_issues_a_session(
    db: AsyncSession, operator_account: Operator
) -> None:
    operator, token, expires_at = await AuthService(db).login(
        operator_account.username, PASSWORD
    )
    assert operator.id == operator_account.id
    assert token
    assert expires_at > datetime.now(UTC)


async def test_the_raw_token_is_never_stored(
    db: AsyncSession, operator_account: Operator
) -> None:
    """A database disclosure must not hand over live sessions."""
    _, token, _ = await AuthService(db).login(operator_account.username, PASSWORD)
    stored = await db.execute(
        text("SELECT token_hash FROM operator_sessions WHERE operator_id = :id"),
        {"id": operator_account.id},
    )
    hashes = [row[0] for row in stored]
    assert token not in hashes
    assert hash_token(token) in hashes


async def test_a_session_token_resolves_to_its_operator(
    db: AsyncSession, operator_account: Operator
) -> None:
    _, token, _ = await AuthService(db).login(operator_account.username, PASSWORD)
    resolved = await AuthService(db).resolve(token)
    assert resolved is not None
    assert resolved.id == operator_account.id


async def test_a_wrong_password_is_refused(
    db: AsyncSession, operator_account: Operator
) -> None:
    from app.services.auth_service import AuthenticationError

    with pytest.raises(AuthenticationError):
        await AuthService(db).login(operator_account.username, "wrong")


async def test_an_unknown_username_is_refused(db: AsyncSession) -> None:
    from app.services.auth_service import AuthenticationError

    with pytest.raises(AuthenticationError):
        await AuthService(db).login("no-such-operator-" + uuid4().hex, PASSWORD)


async def test_the_reserved_legacy_operator_cannot_log_in(db: AsyncSession) -> None:
    """It exists to be attributed to, not to be signed into.

    Its password_hash is a sentinel no password produces, so there is nothing
    to guess -- but that is only true while nobody sets a real one on it.
    """
    from app.services.auth_service import AuthenticationError

    legacy = await AuthService(db).get_by_username(LEGACY_OPERATOR_USERNAME)
    assert legacy is not None, "migration 0010 seeds this row"
    assert not legacy.can_log_in
    with pytest.raises(AuthenticationError):
        await AuthService(db).login(LEGACY_OPERATOR_USERNAME, "!")


async def test_a_revoked_session_stops_resolving(
    db: AsyncSession, operator_account: Operator
) -> None:
    auth = AuthService(db)
    _, token, _ = await auth.login(operator_account.username, PASSWORD)
    assert await auth.revoke(token) is True
    assert await auth.resolve(token) is None
    # Revoking twice is not an error, but only the first call did anything.
    assert await auth.revoke(token) is False


async def test_an_expired_session_stops_resolving(
    db: AsyncSession, operator_account: Operator
) -> None:
    """Written with an expiry in the past rather than waiting twelve hours."""
    auth = AuthService(db)
    token = "expired-" + uuid4().hex
    db.add(
        OperatorSession(
            operator_id=operator_account.id,
            token_hash=hash_token(token),
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    await db.commit()
    assert await auth.resolve(token) is None


async def test_a_deactivated_operator_stops_resolving(
    db: AsyncSession, operator_account: Operator
) -> None:
    """Deactivation has to end sessions already in flight, not just logins."""
    auth = AuthService(db)
    _, token, _ = await auth.login(operator_account.username, PASSWORD)
    operator_account.is_active = False
    await db.commit()
    assert await auth.resolve(token) is None


def test_the_shared_api_key_still_authenticates(
    client: TestClient, admin_headers: dict[str, str], requires_database: None
) -> None:
    """The backward-compatibility guarantee, as a test rather than a claim.

    The Flutter client stores ADMIN_API_KEY and has no login screen, so this
    breaking is the app going dark.
    """
    response = client.get("/admin/auth/me", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["via_legacy_key"] is True
    assert body["operator_id"] is None
    assert body["username"] == LEGACY_OPERATOR_USERNAME


def test_an_unauthenticated_request_is_refused(client: TestClient) -> None:
    assert client.get("/admin/auth/me").status_code == 401


def test_a_wrong_api_key_is_refused(client: TestClient) -> None:
    response = client.get("/admin/auth/me", headers={"X-API-Key": "nope"})
    assert response.status_code == 401


def test_a_bad_bearer_token_is_refused(client: TestClient, requires_database: None) -> None:
    response = client.get(
        "/admin/auth/me", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401


def test_login_rejects_bad_credentials_over_http(
    client: TestClient, requires_database: None
) -> None:
    response = client.post(
        "/admin/auth/login",
        json={"username": "no-such-operator", "password": "whatever"},
    )
    assert response.status_code == 401
