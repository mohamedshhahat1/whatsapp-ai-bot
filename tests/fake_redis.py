"""A small in-memory stand-in for redis.asyncio, good enough for quota tests.

This is a fake, not a mock. It implements the semantics the code under test
actually relies on -- sorted sets, NX adds, TTLs, pipelines -- against a
controllable clock, so the tests can assert on behaviour ("the thirteenth
message this minute is refused") rather than on call sequences ("zcount was
called"). Asserting the latter would pass just as happily against a
reimplementation that was subtly wrong.

Only the commands app/core/quota.py and app/core/idempotency.py use are
implemented. Anything else raises AttributeError loudly rather than silently
returning None, so a test cannot accidentally pass because a command did
nothing.
"""

from __future__ import annotations

import fnmatch
from typing import Any


class FakeRedisError(RuntimeError):
    """Raised by FakeRedis when it is configured to be unavailable."""


class _Pipeline:
    """Queues commands and replays them on execute().

    Real pipelines are also atomic when transaction=True. That distinction does
    not matter here: the tests are single-threaded, so sequential replay
    produces the same observable result.
    """

    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._queued: list[tuple[str, tuple, dict]] = []

    async def __aenter__(self) -> _Pipeline:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def __getattr__(self, name: str) -> Any:
        if not hasattr(self._redis, name):
            raise AttributeError(f"FakeRedis does not implement {name!r}")

        def queue(*args: object, **kwargs: object) -> _Pipeline:
            self._queued.append((name, args, kwargs))
            return self

        return queue

    async def execute(self) -> list[Any]:
        results = []
        for name, args, kwargs in self._queued:
            results.append(await getattr(self._redis, name)(*args, **kwargs))
        self._queued.clear()
        return results


class FakeRedis:
    """In-memory Redis with an injectable clock.

    The clock is the point of the whole thing. Sliding windows cannot be tested
    against wall time without sleeping, and a test suite that sleeps for an
    hour to prove the hourly window works is a test suite nobody runs.
    """

    def __init__(self, clock: Any, *, unavailable: bool = False) -> None:
        self._clock = clock
        self._unavailable = unavailable
        self._values: dict[str, Any] = {}
        self._zsets: dict[str, dict[str, float]] = {}
        self._expires: dict[str, float] = {}
        self.closed = False

    # -- internals ---------------------------------------------------------

    def _guard(self) -> None:
        if self._unavailable:
            raise FakeRedisError("connection refused")

    def _sweep(self) -> None:
        """Drop expired keys. Redis does this lazily too."""
        now = self._clock()
        for key, expiry in list(self._expires.items()):
            if expiry <= now:
                self._expires.pop(key, None)
                self._values.pop(key, None)
                self._zsets.pop(key, None)

    def _live(self, key: str) -> bool:
        self._sweep()
        return key in self._values or key in self._zsets

    # -- strings -----------------------------------------------------------

    async def get(self, key: str) -> str | None:
        self._guard()
        self._sweep()
        value = self._values.get(key)
        return None if value is None else str(value)

    async def set(
        self, key: str, value: object, ex: int | None = None, nx: bool = False
    ) -> bool | None:
        self._guard()
        self._sweep()
        if nx and key in self._values:
            return None
        self._values[key] = value
        if ex is not None:
            self._expires[key] = self._clock() + ex
        return True

    async def setex(self, key: str, seconds: int, value: object) -> bool:
        self._guard()
        self._values[key] = value
        self._expires[key] = self._clock() + seconds
        return True

    async def delete(self, *keys: str) -> int:
        self._guard()
        self._sweep()
        removed = 0
        for key in keys:
            if self._values.pop(key, None) is not None:
                removed += 1
            elif self._zsets.pop(key, None) is not None:
                removed += 1
            self._expires.pop(key, None)
        return removed

    async def exists(self, key: str) -> int:
        self._guard()
        return 1 if self._live(key) else 0

    async def ttl(self, key: str) -> int:
        self._guard()
        self._sweep()
        if not self._live(key):
            return -2
        expiry = self._expires.get(key)
        if expiry is None:
            return -1
        return max(0, int(expiry - self._clock()))

    async def incr(self, key: str) -> int:
        self._guard()
        self._sweep()
        current = int(self._values.get(key, 0))
        self._values[key] = current + 1
        return current + 1

    async def incrby(self, key: str, amount: int) -> int:
        self._guard()
        self._sweep()
        current = int(self._values.get(key, 0))
        self._values[key] = current + amount
        return current + amount

    async def incrbyfloat(self, key: str, amount: float) -> float:
        self._guard()
        self._sweep()
        current = float(self._values.get(key, 0.0))
        self._values[key] = current + amount
        return current + amount

    async def expire(self, key: str, seconds: int) -> bool:
        self._guard()
        if not self._live(key):
            return False
        self._expires[key] = self._clock() + seconds
        return True

    # -- sorted sets -------------------------------------------------------

    async def zadd(self, key: str, mapping: dict[str, float], nx: bool = False) -> int:
        self._guard()
        self._sweep()
        zset = self._zsets.setdefault(key, {})
        added = 0
        for member, score in mapping.items():
            if nx and member in zset:
                continue
            if member not in zset:
                added += 1
            zset[member] = score
        return added

    async def zcount(self, key: str, minimum: float, maximum: float) -> int:
        self._guard()
        self._sweep()
        zset = self._zsets.get(key, {})
        return sum(1 for score in zset.values() if minimum <= score <= maximum)

    async def zremrangebyscore(self, key: str, minimum: float, maximum: float) -> int:
        self._guard()
        self._sweep()
        zset = self._zsets.get(key, {})
        doomed = [m for m, s in zset.items() if minimum <= s <= maximum]
        for member in doomed:
            del zset[member]
        return len(doomed)

    # -- scanning ----------------------------------------------------------

    async def scan_iter(self, match: str = "*", count: int = 100):
        self._guard()
        self._sweep()
        for key in list(self._values) + list(self._zsets):
            if fnmatch.fnmatch(key, match):
                yield key

    # -- lifecycle ---------------------------------------------------------

    def pipeline(self, transaction: bool = True) -> _Pipeline:
        return _Pipeline(self)

    async def aclose(self) -> None:
        self.closed = True


class Clock:
    """A manually advanced monotonic clock."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
