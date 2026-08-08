"""Resolving an operator reply's transport, and the 409 when it cannot.

The gap here is narrow but real. tests/test_outbound_routing.py asserts
``ChannelUnavailableError.status_code == 409`` as a class attribute and never
observes an actual 409 leaving the application, because when it was written
conftest could only build WhatsApp customers. It can build others now, so the
refusal is finally tested through the real endpoint.

Also covers ``outbound_adapter``'s default-settings branch, which every
existing test skips by passing ``settings=`` explicitly -- leaving the call
shape production actually uses unexercised.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels import outbound, registry
from app.channels.base import BaseChannelAdapter
from app.channels.config import ChannelSettings
from app.channels.constants import MESSENGER, WHATSAPP
from app.channels.messenger import MessengerAdapter
from app.channels.outbound import (
    ChannelUnavailableError,
    outbound_adapter,
    recipient_id,
)
from app.channels.whatsapp import WhatsAppAdapter
from app.config import get_settings
from app.integrations.whatsapp import WhatsAppClient
from app.models.user import User
from app.repositories.message import MessageRepository
from tests.conftest import Customer, run_db

_BECOME_MESSENGER = "UPDATE conversations SET channel = 'messenger' WHERE id = :id"


@pytest.fixture
def whatsapp_client() -> WhatsAppClient:
    """A real client that never reaches the network.

    Nothing here calls ``_post`` without replacing it first, so no connection
    is opened and there is nothing to close.
    """
    return WhatsAppClient(get_settings())


@pytest.fixture
def only_real_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the registry to the adapters this repository ships.

    tests/test_channels.py registers a fake adapter into the module-level
    mapping and never removes it, so without this the outcome depends on
    which file pytest ran first.
    """
    monkeypatch.setattr(
        registry,
        "_ADAPTERS",
        {WHATSAPP: WhatsAppAdapter, MESSENGER: MessengerAdapter},
    )


def _configured() -> ChannelSettings:
    """Messenger switched on and fully credentialled, from explicit values."""
    return ChannelSettings(
        _env_file=None,
        enable_messenger=True,
        facebook_page_id="page-1",
        facebook_page_access_token="page-token",
    )


# --- The default settings branch --------------------------------------------


async def test_the_adapter_falls_back_to_the_deployment_settings(
    whatsapp_client: WhatsAppClient,
    only_real_adapters: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Called with no settings, which is how the reply path calls it.

    Every other test supplies ``settings=``, so this branch had never run. A
    ``get_channel_settings`` that stopped resolving here would be discovered
    by an operator rather than by CI.
    """
    monkeypatch.setattr(outbound, "get_channel_settings", _configured)

    adapter = outbound_adapter(MESSENGER, whatsapp_client=whatsapp_client)
    try:
        assert isinstance(adapter, MessengerAdapter)
        assert adapter.channel == MESSENGER
    finally:
        await adapter.aclose()


def test_the_fallback_settings_can_still_refuse(
    whatsapp_client: WhatsAppClient,
    only_real_adapters: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback is a real read, not a permissive shortcut.

    If the default path ever resolved to something optimistic, a deployment
    with Messenger switched off would start trying to send on it.
    """
    monkeypatch.setattr(
        outbound,
        "get_channel_settings",
        lambda: ChannelSettings(_env_file=None),
    )
    with pytest.raises(ChannelUnavailableError):
        outbound_adapter(MESSENGER, whatsapp_client=whatsapp_client)


# --- Registry behaviour ------------------------------------------------------


def test_an_unregistered_channel_resolves_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """outbound_adapter's ``cls is None`` guard is only reachable if this holds.

    Were a missing adapter to raise KeyError instead, the operator would get a
    500 for what is a deployment state.
    """
    monkeypatch.setattr(registry, "_ADAPTERS", {})
    assert registry.adapter_class(MESSENGER) is None


def test_re_registering_a_channel_replaces_rather_than_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adapter modules are imported for their side effect, possibly twice.

    ``_register_adapters`` runs on every resolution. If registration appended
    instead of replacing, which adapter answered would depend on import order.
    """
    monkeypatch.setattr(registry, "_ADAPTERS", {})

    class First(BaseChannelAdapter):
        channel = MESSENGER

        def parse(self, payload: dict[str, Any]) -> list[Any]:
            return []

        async def send_text(self, recipient: str, text: str) -> dict[str, Any]:
            return {}

    class Second(First):
        pass

    registry.register_adapter(First)
    registry.register_adapter(Second)
    assert registry.adapter_class(MESSENGER) is Second


# --- Addressing --------------------------------------------------------------


def test_an_empty_external_id_falls_back_to_the_phone_number() -> None:
    """Migration 0013 made external_id NOT NULL, so "" is now the empty shape.

    None was the only empty value the original fallback was written against.
    Treating "" as present would address the customer as the empty string and
    report the send as a success.
    """
    user = User(channel=WHATSAPP, external_id="", wa_id="20100000000")
    assert recipient_id(user) == "20100000000"


def test_a_row_with_neither_id_is_unaddressable() -> None:
    assert recipient_id(User(channel=MESSENGER, external_id="", wa_id=None)) is None


# --- The 409, over HTTP ------------------------------------------------------


def test_replying_on_an_unavailable_channel_is_a_409(
    client: TestClient,
    admin_headers: dict[str, str],
    sync_customer: Customer,
) -> None:
    """The refusal observed as a real HTTP response, not a class attribute.

    Two things make this test worth more than its length suggests.

    First, the fresh inbound message. This endpoint already answers 409 when
    the 24-hour service window has closed, and a conversation with no inbound
    message at all is outside it -- so a test that skipped this step would go
    green on the wrong refusal and would stay green if channel resolution were
    deleted outright. Writing a message holds the window open so the request
    survives long enough to reach the transport lookup.

    Second, the assertions. The status alone cannot tell the two refusals
    apart, so this checks that the body names the channel and is not the
    template error. Both of ChannelUnavailableError's wordings -- "switched
    off" for a disabled channel and "not configured" for one enabled without
    credentials -- name it, which keeps the test honest on a developer machine
    whose .env happens to enable Messenger.
    """

    async def prepare(session: AsyncSession) -> None:
        await MessageRepository(session).create(
            conversation_id=sync_customer.conversation_id,
            direction="inbound",
            content="Do you deliver on Sundays?",
            wa_message_id=f"wamid.{sync_customer.wa_id}",
        )
        await session.execute(
            text(_BECOME_MESSENGER),
            {"id": sync_customer.conversation_id},
        )
        await session.commit()

    run_db(prepare)

    response = client.post(
        f"/admin/conversations/{sync_customer.conversation_id}/reply",
        headers=admin_headers,
        json={"text": "Yes, we do."},
    )

    assert response.status_code == 409
    body = response.text.lower()
    assert MESSENGER in body, body
    # Not the closed-window 409, which is the other way to get a 409 here.
    assert "template" not in body, body
