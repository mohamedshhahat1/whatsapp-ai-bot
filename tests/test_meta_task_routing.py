"""The Celery Meta task picks its adapters from the delivery.

Two properties are covered here.

The first is an ORDERING, not a return value: the adapters have to be resolved
before the database engine and the OpenAI client are created. If they were
resolved after, an unservable delivery would open a connection pool and an
HTTP client only to discard both, on every retry, five times per payload.
Each of those tests asserts it by making ``create_async_engine`` fail. Reaching
it is the failure, so the assertion cannot silently stop being true the way an
equality check against a mock can.

The second is that a delivery carrying two surfaces reaches both of them. One
``page`` envelope can hold a Messenger message under ``messaging`` and a
Facebook comment under ``changes``; each adapter reads only the array it
understands, so the payload has to be handed to each in turn.

No database and no broker: the first three cases return before anything is
opened, and the last replaces the resources it would have opened.
"""

import asyncio
from typing import Any

import pytest

from app.workers import tasks


def _explode(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError(
        "resources were created for a delivery this deployment cannot serve"
    )


def test_an_unserved_object_returns_before_any_resource_is_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Meta adds products to an existing subscription without asking."""
    monkeypatch.setattr(tasks, "create_async_engine", _explode)
    asyncio.run(tasks._run_meta({"object": "whatsapp_business_account"}))


def test_a_switched_off_channel_returns_before_any_resource_is_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The route checked the same switch, but a queue hop separates the two and
    configuration can change in between."""
    monkeypatch.setattr(tasks, "create_async_engine", _explode)
    asyncio.run(tasks._run_meta({"object": "instagram"}))


def test_a_delivery_with_no_object_returns_before_any_resource_is_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A payload that reached the queue before a deploy changed the route."""
    monkeypatch.setattr(tasks, "create_async_engine", _explode)
    asyncio.run(tasks._run_meta({}))


# --- Per-surface dispatch ---------------------------------------------------


class _FakeAdapter:
    """Stands in for a channel adapter, recording only what the task does."""

    def __init__(self, channel: str) -> None:
        self.channel = channel
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _FakeSession:
    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeEngine:
    async def dispose(self) -> None:
        return None


class _FakeOpenAI:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def aclose(self) -> None:
        return None


async def test_every_surface_in_one_delivery_is_processed_then_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page delivery reaches Messenger and Facebook comments, in that order.

    The real ``_run_meta`` loop runs here; only the resources it opens are
    replaced, because a Postgres pool and an OpenAI client are not what is
    being tested. What is being tested is that each resolved surface receives
    the payload exactly once, private surface first, and that every adapter is
    closed even though they were opened together -- a leaked httpx client per
    delivery would exhaust the worker's file descriptors under load.
    """
    adapters = [_FakeAdapter("messenger"), _FakeAdapter("facebook_comment")]
    seen: list[str] = []

    async def fake_process(
        session: object,
        adapter: Any,
        ai: object,
        settings: object,
        payload: dict[str, Any],
    ) -> None:
        seen.append(adapter.channel)

    def fake_engine(*args: Any, **kwargs: Any) -> Any:
        return _FakeEngine()

    def fake_sessionmaker(*args: Any, **kwargs: Any) -> Any:
        return _FakeSession

    monkeypatch.setattr(tasks, "meta_inbound_adapters", lambda _: adapters)
    monkeypatch.setattr(tasks, "create_async_engine", fake_engine)
    monkeypatch.setattr(tasks, "async_sessionmaker", fake_sessionmaker)
    monkeypatch.setattr(tasks, "OpenAIClient", _FakeOpenAI)
    monkeypatch.setattr(tasks, "process_meta_payload", fake_process)

    await tasks._run_meta({"object": "page"})

    assert seen == ["messenger", "facebook_comment"]
    assert [adapter.closed for adapter in adapters] == [True, True]


async def test_one_surface_failing_still_closes_every_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The error propagates so Celery can retry, but nothing is left open.

    Retrying the whole delivery is safe -- the inbound claim and the outbound
    reservation both key off the provider message id -- but only if the failed
    attempt released its clients first.
    """
    adapters = [_FakeAdapter("messenger"), _FakeAdapter("facebook_comment")]

    async def fake_process(
        session: object,
        adapter: Any,
        ai: object,
        settings: object,
        payload: dict[str, Any],
    ) -> None:
        raise RuntimeError("the graph api fell over")

    def fake_engine(*args: Any, **kwargs: Any) -> Any:
        return _FakeEngine()

    def fake_sessionmaker(*args: Any, **kwargs: Any) -> Any:
        return _FakeSession

    monkeypatch.setattr(tasks, "meta_inbound_adapters", lambda _: adapters)
    monkeypatch.setattr(tasks, "create_async_engine", fake_engine)
    monkeypatch.setattr(tasks, "async_sessionmaker", fake_sessionmaker)
    monkeypatch.setattr(tasks, "OpenAIClient", _FakeOpenAI)
    monkeypatch.setattr(tasks, "process_meta_payload", fake_process)

    with pytest.raises(RuntimeError):
        await tasks._run_meta({"object": "page"})

    assert [adapter.closed for adapter in adapters] == [True, True]
