"""Route-level tests for /webhook/meta.

The adapter and the payload normaliser are covered in tests/test_messenger.py.
What is tested here is only what the route itself decides -- and mostly the
ORDER it decides things in, because that is where the mistakes are invisible:

* A bad signature is refused before the body is parsed. Parsing unverified
  input is the exact thing a signature exists to avoid.
* A switched-off channel and an unrecognised product both answer 200. Meta
  delivers to a subscribed webhook regardless of what this application
  thinks, and treats anything that is not a 200 as a delivery worth repeating
  for hours. A tidy-looking 403 here is a retry storm.
* Only a genuine page delivery reaches the processor, and it arrives
  unchanged.

The handshake is deliberately tested with Messenger switched OFF, because
that is the state an operator is in while they are still configuring it.

None of this needs a database, so these run everywhere instead of skipping on
a machine without Postgres.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.channels.config import ChannelSettings
from app.routers import meta_webhook

# Set in conftest.py before app is imported. ChannelSettings leaves
# FACEBOOK_VERIFY_TOKEN empty, so verify_token() falls back to this one --
# which is the single-Meta-app setup the fallback exists for.
VERIFY_TOKEN = "test-verify-token"

PAGE_DELIVERY: dict[str, Any] = {
    "object": "page",
    "entry": [
        {
            "id": "100000000000001",
            "messaging": [
                {
                    "sender": {"id": "7654321098765432"},
                    "recipient": {"id": "100000000000001"},
                    "timestamp": 1735689600000,
                    "message": {"mid": "m_route_test", "text": "hello"},
                }
            ],
        }
    ],
}


@pytest.fixture
def messenger_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch Messenger on for the route, without touching the environment."""
    channels = ChannelSettings(enable_messenger=True)
    monkeypatch.setattr(meta_webhook, "get_channel_settings", lambda: channels)


@pytest.fixture
def messenger_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the channel off rather than relying on it being off by default."""
    channels = ChannelSettings(enable_messenger=False)
    monkeypatch.setattr(meta_webhook, "get_channel_settings", lambda: channels)


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture whatever the route hands on, by whichever dispatch path.

    Both are stubbed so these tests do not depend on how USE_TASK_QUEUE
    happens to be set: an empty list means nothing was dispatched, not that
    the other branch ran.
    """
    captured: list[dict[str, Any]] = []

    async def inline(payload: dict[str, Any]) -> None:
        captured.append(payload)

    monkeypatch.setattr(meta_webhook, "_process_inline", inline)
    monkeypatch.setattr(
        meta_webhook.process_meta_webhook_event, "delay", captured.append
    )
    return captured


def test_the_handshake_echoes_the_challenge(
    client: TestClient, messenger_off: None
) -> None:
    """Answered while the channel is still switched off, on purpose.

    The handshake proves ownership of the endpoint and echoes back a value
    Meta just supplied; it moves no customer data. Refusing it while disabled
    would force an operator to enable a channel before they could finish
    configuring it, which is the wrong order to make anyone work in.
    """
    response = client.get(
        "/webhook/meta",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1158201444",
        },
    )
    assert response.status_code == 200
    assert response.text == "1158201444"


def test_the_handshake_is_refused_with_the_wrong_token(client: TestClient) -> None:
    response = client.get(
        "/webhook/meta",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "not-the-token",
            "hub.challenge": "1158201444",
        },
    )
    assert response.status_code == 403


def test_the_handshake_needs_the_subscribe_mode(client: TestClient) -> None:
    """A correct token is not on its own a handshake."""
    response = client.get(
        "/webhook/meta",
        params={
            "hub.mode": "unsubscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1158201444",
        },
    )
    assert response.status_code == 403


def test_a_delivery_is_dropped_while_messenger_is_switched_off(
    client: TestClient, messenger_off: None, delivered: list[dict[str, Any]]
) -> None:
    """Acknowledged and thrown away -- not refused.

    The switch has to be enforced here rather than assumed, because Meta keeps
    delivering to a subscribed webhook whatever this application thinks.
    """
    response = client.post("/webhook/meta", json=PAGE_DELIVERY)
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert delivered == []


def test_a_bad_signature_is_refused_before_the_body_is_parsed(
    client: TestClient,
    messenger_on: None,
    delivered: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 for a body that is also unparseable.

    The 400 in the test below shares this exact body. Getting a 403 here and a
    400 there is what proves the signature is checked first: if the two checks
    were ever swapped, this case would come back a parse error, and the
    application would have parsed input it could not vouch for.
    """
    monkeypatch.setattr(meta_webhook, "verify_meta_signature", lambda *a, **k: False)
    response = client.post(
        "/webhook/meta",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 403
    assert delivered == []


def test_a_delivery_for_another_product_is_acknowledged_and_dropped(
    client: TestClient, messenger_on: None, delivered: list[dict[str, Any]]
) -> None:
    """Instagram and the comment surfaces arrive on this same URL.

    They are not wired yet, and answering a commenter with an apology would be
    worse than the silence.
    """
    response = client.post(
        "/webhook/meta", json={"object": "instagram", "entry": []}
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert delivered == []


def test_malformed_json_is_a_client_error(
    client: TestClient, messenger_on: None, delivered: list[dict[str, Any]]
) -> None:
    """A signed body that will not parse is the sender's fault, so 400.

    Unlike the drops above, this one is safe to refuse: a payload Meta cannot
    serialise is not going to succeed on the retry either.
    """
    response = client.post(
        "/webhook/meta",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert delivered == []


def test_a_page_delivery_is_handed_over_intact(
    client: TestClient, messenger_on: None, delivered: list[dict[str, Any]]
) -> None:
    """ACK immediately, and pass the payload on exactly as it arrived.

    The route does no interpretation of its own -- that belongs to the
    normaliser -- so anything it changed here would be a change nobody
    downstream is expecting.
    """
    response = client.post("/webhook/meta", json=PAGE_DELIVERY)
    assert response.status_code == 200
    assert response.json() == {"status": "queued"}
    assert delivered == [PAGE_DELIVERY]
