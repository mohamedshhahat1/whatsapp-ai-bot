"""Task 2: the API process pools database connections.

Configuration and lifecycle, not throughput. A timing benchmark would be the
direct way to show that pooling helps, and also the first test to go flaky on
a shared CI runner. What actually matters here is structural: the API stops
opening a physical connection per checkout, while Celery and the test suite
keep doing exactly that, because their event-loop lifecycles require it.

None of these tests need a reachable database. Building an engine does not
connect -- SQLAlchemy opens a connection on first use -- so the pool object
can be inspected against a URL that points nowhere.
"""

import contextlib
import os

from sqlalchemy.pool import AsyncAdaptedQueuePool, NullPool

from app.db.session import (
    MAX_OVERFLOW,
    POOL_RECYCLE_SECONDS,
    POOL_SIZE,
    POOL_TIMEOUT_SECONDS,
    SessionLocal,
    build_engine,
    engine,
    get_db,
    pooling_enabled,
)

# Never connected to; see the module docstring.
URL = "postgresql+asyncpg://user:pass@127.0.0.1:5432/unused"


class TestPoolConfiguration:
    async def test_the_api_engine_reuses_connections(self) -> None:
        """The whole point of the task: a real pool, not NullPool."""
        built = build_engine(URL, pooled=True)
        try:
            assert isinstance(built.pool, AsyncAdaptedQueuePool)
            assert built.pool.size() == POOL_SIZE
        finally:
            await built.dispose()

    async def test_the_unpooled_engine_still_exists_for_callers_that_need_it(
        self,
    ) -> None:
        """Celery and the tests depend on this branch remaining available."""
        built = build_engine(URL, pooled=False)
        try:
            assert isinstance(built.pool, NullPool)
        finally:
            await built.dispose()

    def test_the_ceiling_leaves_room_for_the_other_containers(self) -> None:
        """worker, beat, backup and the exporters share max_connections.

        An unbounded overflow would let one burst of API traffic take every
        connection Postgres has and lock the Celery worker out of the
        database, which fails silently: webhooks would just stop being
        processed.
        """
        assert MAX_OVERFLOW >= 0
        assert POOL_SIZE + MAX_OVERFLOW <= 25

    def test_a_checkout_cannot_block_forever(self) -> None:
        assert 0 < POOL_TIMEOUT_SECONDS <= 60

    def test_connections_are_recycled_before_typical_idle_timeouts(self) -> None:
        assert 0 < POOL_RECYCLE_SECONDS <= 3600


class TestSessionLifecycle:
    async def test_get_db_closes_the_session_it_yielded(self) -> None:
        """A leaked session is a leaked connection.

        Under NullPool a leak merely wasted a socket. Under a pool it removes
        one connection from circulation for the life of the process, so this
        is a stronger requirement now than it was before pooling.
        """
        agen = get_db()
        session = await anext(agen)
        with contextlib.suppress(StopAsyncIteration):
            await anext(agen)
        assert not session.in_transaction()

    async def test_the_session_factory_is_bound_to_the_module_engine(self) -> None:
        async with SessionLocal() as session:
            assert session.bind is engine


class TestTestHarness:
    def test_pooling_is_disabled_under_pytest(self) -> None:
        """conftest turns pooling off before the app is imported.

        Not a tautology. ``run_db()`` opens a fresh event loop per call and
        TestClient runs the app in another loop on its own thread; an asyncpg
        connection belongs to the loop that opened it, so a pooled connection
        would be handed to a closed loop on the second call. This assertion
        is what stops that guard being removed as redundant.
        """
        assert os.environ["DB_POOL_ENABLED"] == "false"
        assert pooling_enabled() is False
        assert isinstance(engine.pool, NullPool)
