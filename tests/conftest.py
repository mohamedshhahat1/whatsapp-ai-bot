"""Shared test fixtures.

Environment variables are set before ``app`` is imported: Settings is a cached
pydantic-settings object built at import time, so anything set afterwards
would be ignored.
"""

import os

os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("WHATSAPP_APP_SECRET", "")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("USE_TASK_QUEUE", "false")

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import SessionLocal, engine
from app.main import app
from app.repositories.conversation import ConversationRepository
from app.repositories.user import UserRepository

# Deleting a customer by hand rather than relying on cascades: the FKs are
# plain references, so the children have to go first.
_DELETE_AI_LOGS = """
DELETE FROM ai_logs WHERE conversation_id IN (
    SELECT c.id FROM conversations c
    JOIN users u ON u.id = c.user_id
    WHERE u.wa_id = :wa_id
)
"""

_DELETE_MESSAGES = """
DELETE FROM messages WHERE conversation_id IN (
    SELECT c.id FROM conversations c
    JOIN users u ON u.id = c.user_id
    WHERE u.wa_id = :wa_id
)
"""

_DELETE_CONVERSATIONS = """
DELETE FROM conversations WHERE user_id IN (
    SELECT id FROM users WHERE wa_id = :wa_id
)
"""

_DELETE_USERS = "DELETE FROM users WHERE wa_id = :wa_id"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"X-API-Key": get_settings().admin_api_key}


def database_reachable() -> bool:
    """Check connectivity in a throwaway event loop.

    The engine is disposed afterwards so no connection pool stays bound to a
    loop that is about to be closed.
    """

    async def check() -> bool:
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    return asyncio.run(check())


def run_db[T](operation: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run one database operation from synchronous test code.

    A synchronous test that also uses TestClient cannot share a pool with the
    application: TestClient runs the app in its own event loop on another
    thread, and asyncpg connections belong to the loop that opened them. Each
    call therefore gets a fresh pool and disposes it on the way out.
    """

    async def runner() -> T:
        try:
            async with SessionLocal() as session:
                return await operation(session)
        finally:
            await engine.dispose()

    return asyncio.run(runner())


@pytest.fixture
def requires_database() -> None:
    """Skip a synchronous test when no database is configured."""
    if not database_reachable():
        pytest.skip("No database reachable at DATABASE_URL")


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """A session against the real database.

    CI provisions Postgres and applies the migrations before pytest runs, so
    these tests exercise the actual schema, indexes and ON CONFLICT clauses.
    The engine is disposed afterwards because pytest-asyncio gives each test
    its own event loop, and an asyncpg pool cannot be reused across loops.

    Do not combine this fixture with TestClient in one test; use ``run_db``
    from a synchronous test instead.
    """
    if not database_reachable():
        pytest.skip("No database reachable at DATABASE_URL")
    try:
        async with SessionLocal() as session:
            yield session
    finally:
        await engine.dispose()


@dataclass
class Customer:
    """A throwaway customer with an open conversation."""

    wa_id: str
    user_id: int
    conversation_id: int


async def purge(session: AsyncSession, wa_id: str) -> None:
    """Remove a test customer and everything hanging off it."""
    params = {"wa_id": wa_id}
    for statement in (
        _DELETE_AI_LOGS,
        _DELETE_MESSAGES,
        _DELETE_CONVERSATIONS,
        _DELETE_USERS,
    ):
        await session.execute(text(statement), params)
    await session.commit()


async def create_customer(session: AsyncSession, wa_id: str) -> Customer:
    """Create a customer with an open conversation, through the repositories."""
    user = await UserRepository(session).get_or_create(wa_id=wa_id, name="Test User")
    conversation = await ConversationRepository(session).get_or_create_active(user.id)
    await session.commit()
    return Customer(
        wa_id=wa_id,
        user_id=user.id,
        conversation_id=conversation.id,
    )


def new_wa_id() -> str:
    """A unique WhatsApp id, so tests never collide on the unique index."""
    return "test-" + uuid4().hex[:12]


@pytest.fixture
async def customer(db: AsyncSession) -> AsyncIterator[Customer]:
    """A customer for async tests, sharing the ``db`` session."""
    wa_id = new_wa_id()
    created = await create_customer(db, wa_id)
    try:
        yield created
    finally:
        await purge(db, wa_id)


@pytest.fixture
def sync_customer(requires_database: None) -> Iterator[Customer]:
    """A customer for synchronous tests that drive the API with TestClient."""
    wa_id = new_wa_id()
    created = run_db(lambda session: create_customer(session, wa_id))
    try:
        yield created
    finally:
        run_db(lambda session: purge(session, wa_id))
