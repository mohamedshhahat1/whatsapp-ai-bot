"""Async engine and session factory with a FastAPI dependency.

Pooling
-------
The API reuses connections through SQLAlchemy's async queue pool. Before this,
the engine used NullPool, which opens a physical asyncpg connection per
checkout and closes it on return -- a TCP round trip, TLS negotiation and a
Postgres authentication on the hot path of every request.

Two callers keep the unpooled behaviour deliberately:

* Celery tasks never import this module. Each builds its own engine with
  NullPool inside its own ``asyncio.run`` loop and disposes it in a
  ``finally`` -- see ``app/workers/tasks.py``. An asyncpg connection belongs
  to the event loop that opened it, so a pool shared across per-task loops
  would eventually hand out a connection bound to a loop that had closed.

* The test suite sets ``DB_POOL_ENABLED=false`` before importing the app, for
  that same reason: ``tests/conftest.py`` drives synchronous tests through
  ``asyncio.run`` and TestClient runs the app in another loop on a separate
  thread.

``DB_POOL_ENABLED`` is read with ``os.getenv`` rather than added to Settings on
purpose. It is a process-lifecycle switch for the test harness, not a
deployment knob: a Settings field would oblige tests/test_settings_sync.py to
see it documented in ``.env.example``, which would advertise a way to turn
pooling off in production.
"""

import os
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.config import get_settings

# Sized for the deployment in docker-compose.prod.yml: a single API container
# running uvicorn behind nginx. Ten resident connections is generous for an
# async workload whose handlers spend most of their time awaiting OpenAI
# rather than Postgres, and capping overflow at five keeps this process'
# worst case at fifteen. Postgres' max_connections is also drawn on by the
# worker, beat, backup and exporter containers, so an unbounded ceiling here
# would let one burst of API traffic lock the Celery worker out of the
# database.
POOL_SIZE = 10
MAX_OVERFLOW = 5

# Wait this long for a free connection before failing the request. A 500 is
# diagnosable; a request parked forever behind an exhausted pool is not.
POOL_TIMEOUT_SECONDS = 30

# Retire connections comfortably under the idle timeouts that sit between the
# app and Postgres. A connection dropped by an idle proxy still looks alive to
# the pool until something tries to use it.
POOL_RECYCLE_SECONDS = 1800

_DISABLED = {"0", "false", "no", "off"}


def pooling_enabled() -> bool:
    """Whether this process should pool connections. See the module docstring."""
    return os.getenv("DB_POOL_ENABLED", "true").strip().lower() not in _DISABLED


def build_engine(url: str, *, pooled: bool) -> AsyncEngine:
    """Build the engine this process should use.

    ``pool_pre_ping`` is kept in both branches: it is what lets a connection
    severed by a Postgres restart or an idle proxy be replaced transparently,
    rather than failing the unlucky request that happened to draw it.
    """
    if not pooled:
        return create_async_engine(url, pool_pre_ping=True, poolclass=NullPool)
    return create_async_engine(
        url,
        pool_pre_ping=True,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_timeout=POOL_TIMEOUT_SECONDS,
        pool_recycle=POOL_RECYCLE_SECONDS,
    )


engine = build_engine(get_settings().database_url, pooled=pooling_enabled())

SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a database session scoped to a single request.

    Leaving the ``async with`` closes the session, which returns its
    connection to the pool. That return is what makes pooling work at all: a
    handler that held one open would take a connection out of circulation for
    good, and POOL_SIZE + MAX_OVERFLOW such requests would exhaust the pool
    permanently.
    """
    async with SessionLocal() as session:
        yield session
