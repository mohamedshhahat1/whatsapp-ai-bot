"""The Meta webhook, exercised through the real application path.

tests/test_meta_webhook.py covers the Messenger side. This file covers what
changed when the route stopped assuming every delivery was Messenger, and it
goes through the FastAPI app rather than calling the handler directly, because
the thing under test is an ordering that only the real request path enforces:
the signature check, the global switch, the parse and the per-channel switch
have to happen in that sequence or a live deployment either leaks work or
starts answering Meta with retryable errors.

Every POST is signed. CI exports WHATSAPP_APP_SECRET, so the allow_unsigned
escape hatch that makes local runs convenient is shut on the runner -- an
unsigned delivery here would pass locally and 403 in CI.
"""

import hashlib
import hmac
import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.channels.config import ChannelSettings
from app.channels.instagram import InstagramDMAdapter
from app.channels.messenger import MessengerAdapter
from app.channels.outbound import meta_inbound_adapter
from app.routers import meta_webhook

APP_SECRET = "test-meta-app-secret"
IG_ACCOUNT = "17841400000000001"
PAGE_ID = "100000000000001"
CUSTOMER = "6789012345678901"

INSTAGRAM_DELIVERY: dict[str, Any] = {
    "object": "instagram",
    "entry": [
        {
            "id": IG_ACCOUNT,
            "time": 1730000000000,
            "messaging": [
                {
                    "sender": {"id": CUSTOMER},
                    "recipient": {"id": IG_ACCOUNT},
                    "timestamp": 1730000000000,
                    "message": {"mid": "m_ig_1", "text": "hello"},
                }
            ],
        }
    ],
}

PAGE_DELIVERY: dict[str, Any] = {
    "object": "page",
    "entry": [{"id": PAGE_ID, "time": 1730000000000, "messaging": []}],
}


def _body(payload: Any) -> bytes:
    return json.dumps(payload).encode()


def _signed(body: bytes) -> dict[str, str]:
    digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": "sha256=" + digest,
    }


def _channels(**overrides: Any) -> ChannelSettings:
    """Channel settings with credentials for both Meta DM surfaces.

    Credentials are always present so that a dropped delivery in these tests
    is always about a switch, never about a missing token.
    """
    return ChannelSettings(
        _env_file=None,
        meta_app_secret=APP_SECRET,
        facebook_page_id=PAGE_ID,
        facebook_page_access_token="unit-test-placeholder",
        instagram_account_id=IG_ACCOUNT,
        instagram_access_token="unit-test-placeholder",
        **overrides,
    )


@pytest.fixture
def instagram_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Instagram DM on, Messenger off -- the mirror of the existing fixtures."""
    monkeypatch.setattr(
        meta_webhook,
        "get_channel_settings",
        lambda: _channels(enable_instagram_dm=True, enable_messenger=False),
    )


@pytest.fixture
def messenger_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        meta_webhook,
        "get_channel_settings",
        lambda: _channels(enable_instagram_dm=False, enable_messenger=True),
    )


@pytest.fixture
def all_meta_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        meta_webhook,
        "get_channel_settings",
        lambda: _channels(enable_instagram_dm=False, enable_messenger=False),
    )


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Capture whatever the route hands over, on either dispatch path.

    Both are stubbed because which one runs depends on USE_TASK_QUEUE, and a
    test that pinned one would quietly stop covering the route if that setting
    changed.
    """
    captured: list[dict[str, Any]] = []

    async def fake_inline(payload: dict[str, Any]) -> None:
        captured.append(payload)

    monkeypatch.setattr(meta_webhook, "_process_inline", fake_inline)
    monkeypatch.setattr(
        meta_webhook.process_meta_webhook_event,
        "delay",
        lambda payload: captured.append(payload),
    )
    yield captured


# --- Routing by object ------------------------------------------------------


def test_an_instagram_delivery_is_handed_over_intact(
    client: TestClient, instagram_only: None, delivered: list[dict[str, Any]]
) -> None:
    body = _body(INSTAGRAM_DELIVERY)
    response = client.post("/webhook/meta", content=body, headers=_signed(body))

    assert response.status_code == 200
    assert response.json() == {"status": "queued"}
    assert delivered == [INSTAGRAM_DELIVERY]


def test_a_page_delivery_is_dropped_while_only_instagram_is_on(
    client: TestClient, instagram_only: None, delivered: list[dict[str, Any]]
) -> None:
    """The mirror case. Proves the gate is per-channel: enabling Instagram must
    not open the route to Messenger traffic."""
    body = _body(PAGE_DELIVERY)
    response = client.post("/webhook/meta", content=body, headers=_signed(body))

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert delivered == []


def test_an_instagram_delivery_is_dropped_while_instagram_is_off(
    client: TestClient, messenger_only: None, delivered: list[dict[str, Any]]
) -> None:
    body = _body(INSTAGRAM_DELIVERY)
    response = client.post("/webhook/meta", content=body, headers=_signed(body))

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert delivered == []


def test_an_unknown_object_is_acknowledged_and_dropped(
    client: TestClient, instagram_only: None, delivered: list[dict[str, Any]]
) -> None:
    """Meta adds products to an existing subscription. A 4xx here would have it
    retry a payload this app will never serve."""
    body = _body({"object": "whatsapp_business_account", "entry": []})
    response = client.post("/webhook/meta", content=body, headers=_signed(body))

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert delivered == []


def test_a_body_that_is_not_an_object_is_acknowledged_and_dropped(
    client: TestClient, instagram_only: None, delivered: list[dict[str, Any]]
) -> None:
    body = _body([{"object": "instagram"}])
    response = client.post("/webhook/meta", content=body, headers=_signed(body))

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert delivered == []


# --- Ordering ---------------------------------------------------------------


def test_nothing_is_parsed_when_no_meta_surface_is_enabled(
    client: TestClient, all_meta_off: None, delivered: list[dict[str, Any]]
) -> None:
    """A malformed body must still ACK while every Meta channel is off.

    This is the assertion that pins the ordering. If the global switch check
    ever moves below the parse, this body becomes a 400 and Meta starts
    retrying deliveries at a deployment that has deliberately switched every
    Meta surface off.
    """
    body = b"{not json at all"
    response = client.post("/webhook/meta", content=body, headers=_signed(body))

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert delivered == []


def test_malformed_json_is_a_client_error_while_a_surface_is_enabled(
    client: TestClient, instagram_only: None
) -> None:
    body = b"{not json at all"
    response = client.post("/webhook/meta", content=body, headers=_signed(body))

    assert response.status_code == 400


def test_a_bad_signature_is_refused_before_anything_else(
    client: TestClient, instagram_only: None, delivered: list[dict[str, Any]]
) -> None:
    body = _body(INSTAGRAM_DELIVERY)
    response = client.post(
        "/webhook/meta",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
        },
    )

    assert response.status_code == 403
    assert delivered == []


# --- Adapter selection, through the real resolver ---------------------------


async def test_an_instagram_object_selects_the_instagram_adapter() -> None:
    """The contract that keeps channel attribution honest.

    Not mocked: this is the function the route and the Celery task both call.
    If it returned a MessengerAdapter here, every Instagram conversation would
    be written with channel='messenger' and every per-channel analytics figure
    would be wrong at the source.
    """
    adapter = meta_inbound_adapter(
        "instagram",
        settings=_channels(enable_instagram_dm=True),
    )
    assert isinstance(adapter, InstagramDMAdapter)
    assert adapter.channel == "instagram_dm"
    await adapter.aclose()


async def test_a_page_object_still_selects_the_messenger_adapter() -> None:
    """Regression guard: the existing channel must be untouched."""
    adapter = meta_inbound_adapter(
        "page",
        settings=_channels(enable_messenger=True),
    )
    assert isinstance(adapter, MessengerAdapter)
    assert adapter.channel == "messenger"
    await adapter.aclose()


def test_no_adapter_for_an_object_this_app_does_not_serve() -> None:
    assert (
        meta_inbound_adapter(
            "whatsapp_business_account",
            settings=_channels(enable_instagram_dm=True),
        )
        is None
    )


def test_no_adapter_while_the_channel_is_switched_off() -> None:
    assert (
        meta_inbound_adapter(
            "instagram",
            settings=_channels(enable_instagram_dm=False),
        )
        is None
    )


def test_no_adapter_when_enabled_but_unconfigured() -> None:
    """Returns None rather than raising.

    The caller is a webhook or a Celery task. A raised error would become a
    retry -- five of them, with backoff -- for a delivery that cannot become
    servable until somebody edits the environment.
    """
    settings = ChannelSettings(
        _env_file=None,
        meta_app_secret=APP_SECRET,
        enable_instagram_dm=True,
        instagram_account_id="",
        instagram_access_token="",
        facebook_page_access_token="",
    )
    assert meta_inbound_adapter("instagram", settings=settings) is None
