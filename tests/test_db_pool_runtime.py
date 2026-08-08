"""Runtime proof that the pooled engine actually talks to PostgreSQL.

tests/test_db_pool.py is configuration-only by design: it builds engines
against a deliberately unreachable URL and never opens a socket, and
conftest.py sets DB_POOL_ENABLED=false so the shared engine the rest of the
suite uses is a NullPool. Between the two, nothing had ever proved that
build_engine(pooled=True) can check a connection out of a real QueuePool and
run SQL on it -- the pooling feature was covered entirely by assertions about
its own settings.

These tests build their own engine from the URL the suite is already pointed
at, and dispose of it again, so the module-level engine and the harness
setting are left exactly as they were. They skip when no database is
reachable, which is how every other database test here behaves.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.pool import AsyncAdaptedQueuePool

from app.db import session as db_session

pytestmark = pytest.mark.usefixtures("requires_database")


def real_database_url() -> str:
    """The URL the suite is actually pointed at.

    Read off the engine the application built rather than reassembled from
    the environment, so it cannot drift from what every other test uses and
    cannot silently become the fake 'unused' URL the configuration-only
    tests rely on.
    """
    return db_session.engine.url.render_as_string(hide_password=False)


async def test_a_pooled_engine_checks_out_a_real_connection() -> None:
    """The whole point: a QueuePool, a real socket, and real SQL."""
    url = real_database_url()
    assert "unused" not in url

    engine = db_session.build_engine(url, pooled=True)
    try:
        assert isinstance(engine.pool, AsyncAdaptedQueuePool)
        assert engine.pool.size() == db_session.POOL_SIZE

        async with engine.connect() as connection:
            assert engine.pool.checkedout() == 1
            result = await connection.execute(text("SELECT 1"))
            assert result.scalar_one() == 1

        # Leaving the block returns the connection to the pool rather than
        # closing it, which is the behaviour NullPool does not have.
        assert engine.pool.checkedout() == 0
        assert engine.pool.checkedin() == 1
    finally:
        await engine.dispose()


async def test_sequential_checkouts_reuse_one_connection() -> None:
    """Three round trips, one physical connection.

    This is the observable difference between a pool and NullPool: NullPool
    opens and discards a connection per checkout and would report nothing
    checked in at the end.
    """
    engine = db_session.build_engine(real_database_url(), pooled=True)
    try:
        for _ in range(3):
            async with engine.connect() as connection:
                result = await connection.execute(text("SELECT 1"))
                assert result.scalar_one() == 1

        assert engine.pool.checkedin() == 1
        assert engine.pool.checkedout() == 0
    finally:
        await engine.dispose()


async def test_a_session_on_the_pooled_engine_closes() -> None:
    """The application uses sessions, not bare connections."""
    engine = db_session.build_engine(real_database_url(), pooled=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(text("SELECT 1"))
            assert result.scalar_one() == 1

        assert engine.pool.checkedout() == 0
    finally:
        await engine.dispose()


async def test_disposing_a_pooled_engine_is_clean() -> None:
    """dispose() must release everything and leave the engine usable.

    A redeploy that disposed badly would leak server-side sessions, and one
    that poisoned the engine would fail only on the next request.
    """
    engine = db_session.build_engine(real_database_url(), pooled=True)

    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))

    assert engine.pool.checkedin() == 1

    await engine.dispose()

    # dispose() swaps in a fresh pool: nothing is left checked in, and the
    # engine still works afterwards.
    assert engine.pool.checkedin() == 0

    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT 1"))
        assert result.scalar_one() == 1

    await engine.dispose()
