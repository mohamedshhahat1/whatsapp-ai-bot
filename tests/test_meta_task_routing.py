"""The Celery Meta task picks its adapter from the delivery.

The interesting property is an ORDERING, not a return value: the adapter has to
be resolved before the database engine and the OpenAI client are created. If it
were resolved after, an unservable delivery would open a connection pool and an
HTTP client only to discard both, on every retry, five times per payload.

Each test asserts that by making ``create_async_engine`` fail. Reaching it is
the failure, so the assertion cannot silently stop being true the way an
equality check against a mock can.

No database and no broker: both cases return before anything is opened, which
is the point.
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
