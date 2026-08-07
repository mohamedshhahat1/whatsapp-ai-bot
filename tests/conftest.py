"""Shared test fixtures.

Environment variables are set before ``app`` is imported: Settings is a cached
pydantic-settings object built at import time, so anything set afterwards
would be ignored.
"""

import os

os.environ["WHATSAPP_VERIFY_TOKEN"] = "test-verify-token"
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("WHATSAPP_APP_SECRET", "")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("USE_TASK_QUEUE", "false")
# The API pools database connections; the tests must not. run_db() opens a
# fresh event loop per call and TestClient runs the app in another loop on its
# own thread, and an asyncpg connection belongs to the loop that opened it --
# a pooled connection would be handed to a loop that has already closed. Set
# rather than defaulted, because inheriting a stray "true" from the
# environment would break the suite in a way that looks nothing like its
# cause. See app/db/session.py.
os.environ["DB_POOL_ENABLED"] = "false"
# CI may set REDIS_PASSWORD for the integration pipeline, but the unit tests
# that construct Settings(environment="development") must not inherit it.
# Pop it here so test_development_does_not_require_redis_auth sees a clean env.
# test_production_accepts_fully_provided_secrets passes redis_password explicitly.
os.environ.pop("REDIS_PASSWORD", None)

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.constants import MESSENGER, WHATSAPP
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

# The same four deletes keyed on (channel, external_id) instead of on wa_id.
#
# Needed because wa_id is null for everybody who did not arrive on WhatsApp,
# so the statements above match nothing for a Messenger customer and would
# leave rows behind for the next test to trip over. (channel, external_id) is
# the identity the schema actually enforces -- see uq_users_channel_external_id
# and migration 0013 -- so this cleans up any customer on any channel,
# including a WhatsApp one.
_DELETE_AI_LOGS_BY_CHANNEL = """
DELETE FROM ai_logs WHERE conversation_id IN (
    SELECT c.id FROM conversations c
    JOIN users u ON u.id = c.user_id
    WHERE u.channel = :channel AND u.external_id = :external_id
)
"""

_DELETE_MESSAGES_BY_CHANNEL = """
DELETE FROM messages WHERE conversation_id IN (
    SELECT c.id FROM conversations c
    JOIN users u ON u.id = c.user_id
    WHERE u.channel = :channel AND u.external_id = :external_id
)
"""

_DELETE_CONVERSATIONS_BY_CHANNEL = """
DELETE FROM conversations WHERE user_id IN (
    SELECT id FROM users WHERE channel = :channel AND external_id = :external_id
)
"""

_DELETE_USERS_BY_CHANNEL = (
    "DELETE FROM users WHERE channel = :channel AND external_id = :external_id"
)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"X-API-Key": get_settings().admin_api_key}


async def database_reachable() -> bool:
    """Check connectivity. NullPool (set on the engine) means no connections
    are pooled, so the check never leaves a stale connection bound to a
    closed event loop.
    """
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def run_db[T](operation: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run one database operation from synchronous test code.

    A synchronous test that also uses TestClient cannot share a pool with the
    application: TestClient runs the app in its own event loop on another
    thread, and asyncpg connections belong to the loop that opened them.
    NullPool means each call gets a fresh connection with no pool state
    shared across event loops.
    """

    async def runner() -> T:
        async with SessionLocal() as session:
            return await operation(session)

    return asyncio.run(runner())


@pytest.fixture
def requires_database() -> None:
    """Skip a synchronous test when no database is configured."""
    if not asyncio.run(database_reachable()):
        pytest.skip("No database reachable at DATABASE_URL")


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    """A session against the real database.

    CI provisions Postgres and applies the migrations before pytest runs, so
    these tests exercise the actual schema, indexes and ON CONFLICT clauses.

    Do not combine this fixture with TestClient in one test; use ``run_db``
    from a synchronous test instead.
    """
    if not await database_reachable():
        pytest.skip("No database reachable at DATABASE_URL")
    async with SessionLocal() as session:
        yield session


@dataclass
class Customer:
    """A throwaway customer with an open conversation.

    ``wa_id`` is the empty string for a customer who did not arrive on
    WhatsApp, matching what the admin API sends clients rather than inventing
    a page-scoped id in a field every client renders as a phone number.
    ``external_id`` is the one that is always populated.
    """

    wa_id: str
    user_id: int
    conversation_id: int
    channel: str = WHATSAPP
    external_id: str = ""


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


async def purge_channel(session: AsyncSession, channel: str, external_id: str) -> None:
    """Remove a customer identified the way the schema identifies them.

    :func:`purge` is left alone rather than widened: every existing caller
    passes a wa_id, and changing its signature would mean editing dozens of
    tests to fix a gap none of them have.
    """
    params = {"channel": channel, "external_id": external_id}
    for statement in (
        _DELETE_AI_LOGS_BY_CHANNEL,
        _DELETE_MESSAGES_BY_CHANNEL,
        _DELETE_CONVERSATIONS_BY_CHANNEL,
        _DELETE_USERS_BY_CHANNEL,
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
        channel=WHATSAPP,
        external_id=wa_id,
    )


async def create_channel_customer(
    session: AsyncSession,
    channel: str,
    external_id: str,
    name: str | None = "Test User",
) -> Customer:
    """Create a customer on any channel, with an open conversation.

    Goes through the same repository methods production uses, so the row is
    written by the real ON CONFLICT clause against the real unique constraint
    rather than being assembled by the test. The conversation is stamped with
    the channel for the same reason the webhook path stamps it: without it the
    row falls back to the column's server default and reads as WhatsApp.
    """
    user = await UserRepository(session).get_or_create_by_channel(
        channel=channel, external_id=external_id, name=name
    )
    conversation = await ConversationRepository(session).get_or_create_active(
        user.id, channel=channel
    )
    await session.commit()
    return Customer(
        wa_id=user.wa_id or "",
        user_id=user.id,
        conversation_id=conversation.id,
        channel=channel,
        external_id=external_id,
    )


def new_wa_id() -> str:
    """A unique WhatsApp id, so tests never collide on the unique index."""
    return "test-" + uuid4().hex[:12]


def new_external_id(prefix: str = "psid") -> str:
    """A unique page-scoped id, for a customer with no phone number."""
    return f"{prefix}-" + uuid4().hex[:12]


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
async def messenger_customer(db: AsyncSession) -> AsyncIterator[Customer]:
    """A Messenger customer: a page-scoped id and no phone number at all.

    The absence of a wa_id is the point. A Messenger row that happened to
    carry one would let a WhatsApp-shaped code path pass by accident, which is
    the failure this fixture exists to make impossible.
    """
    external_id = new_external_id()
    created = await create_channel_customer(db, MESSENGER, external_id)
    try:
        yield created
    finally:
        await purge_channel(db, MESSENGER, external_id)


@pytest.fixture
def sync_customer(requires_database: None) -> Iterator[Customer]:
    """A customer for synchronous tests that drive the API with TestClient."""
    wa_id = new_wa_id()
    created = run_db(lambda session: create_customer(session, wa_id))
    try:
        yield created
    finally:
        run_db(lambda session: purge(session, wa_id))
