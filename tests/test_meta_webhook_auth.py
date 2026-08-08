"""The signature surface of /webhook/meta.

tests/test_meta_webhook.py covers the handshake, the order the route decides
things in, switched-off channels, unrecognised products and malformed JSON.
What it does not cover is verification itself beyond one well-formed but
incorrect digest: a missing header, a header with no algorithm prefix, and --
the one that matters -- a perfectly valid signature computed over a different
body.

That last case is the only one that proves the digest is compared against the
bytes that actually arrived. A verifier that hashed its own re-serialisation
of the parsed body, or that accepted any syntactically valid header, would
pass every other test in the suite while accepting forged deliveries.

Every signature here is computed over the exact bytes sent, against a secret
the fixture pins on ChannelSettings rather than reading from the environment:
CI sets a real-looking WHATSAPP_APP_SECRET and a developer may have
META_APP_SECRET in a local .env, and a test that answers differently
depending on which is a test that reports the machine rather than the code.

No database, so these run everywhere.
"""

import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.channels.config import ChannelSettings
from app.routers import meta_webhook

APP_SECRET = "test-meta-app-secret"

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
                    "message": {"mid": "m_auth_test", "text": "hello"},
                }
            ],
        }
    ],
}


def _body(payload: Any) -> bytes:
    """Serialise to the exact bytes that will go on the wire.

    Not handed to ``json=``, because the signature covers the raw body and
    letting httpx pick its own separators would produce a digest correct for
    bytes nobody sent.
    """
    return json.dumps(payload).encode()


def _digest(body: bytes) -> str:
    return hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _signed(body: bytes) -> dict[str, str]:
    """The headers Meta would send with ``body``."""
    return {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={_digest(body)}",
    }


@pytest.fixture
def messenger_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch Messenger on for the route, without touching the environment."""
    channels = ChannelSettings(enable_messenger=True, meta_app_secret=APP_SECRET)
    monkeypatch.setattr(meta_webhook, "get_channel_settings", lambda: channels)


@pytest.fixture
def delivered(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture whatever the route hands on, by whichever dispatch path.

    Both are stubbed so an empty list means nothing was dispatched, rather
    than that the other branch ran.
    """
    captured: list[dict[str, Any]] = []

    async def inline(payload: dict[str, Any]) -> None:
        captured.append(payload)

    monkeypatch.setattr(meta_webhook, "_process_inline", inline)
    monkeypatch.setattr(
        meta_webhook.process_meta_webhook_event, "delay", captured.append
    )
    return captured


def test_a_delivery_with_no_signature_header_is_refused(
    client: TestClient, messenger_on: None, delivered: list[dict[str, Any]]
) -> None:
    """An absent header is not the same code path as a wrong one.

    With a secret configured, unsigned input has to be refused: anyone who
    knows the URL could otherwise post a customer message.
    """
    body = _body(PAGE_DELIVERY)
    response = client.post(
        "/webhook/meta",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 403
    assert delivered == []


def test_a_signature_without_its_algorithm_prefix_is_refused(
    client: TestClient, messenger_on: None, delivered: list[dict[str, Any]]
) -> None:
    """The digest is correct; only the ``sha256=`` prefix is missing.

    Stripping the prefix and comparing the remainder would accept a header
    whose algorithm nobody checked.
    """
    body = _body(PAGE_DELIVERY)
    response = client.post(
        "/webhook/meta",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _digest(body),
        },
    )
    assert response.status_code == 403
    assert delivered == []


def test_a_signature_naming_another_algorithm_is_refused(
    client: TestClient, messenger_on: None, delivered: list[dict[str, Any]]
) -> None:
    """sha1= was the header Meta used to send, and it is not accepted here."""
    body = _body(PAGE_DELIVERY)
    response = client.post(
        "/webhook/meta",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha1={_digest(body)}",
        },
    )
    assert response.status_code == 403
    assert delivered == []


def test_a_valid_signature_for_a_different_body_is_refused(
    client: TestClient, messenger_on: None, delivered: list[dict[str, Any]]
) -> None:
    """The tamper case, and the reason this file exists.

    The header is a genuine HMAC made with the real secret -- it is simply the
    HMAC of a different payload. Every other signature test would still pass
    against a verifier that only checked the header was well formed, or that
    hashed its own re-serialisation of the parsed body instead of the received
    bytes. This one would not.
    """
    headers = _signed(_body(PAGE_DELIVERY))
    tampered = _body({"object": "page", "entry": [{"id": "999", "messaging": []}]})

    response = client.post("/webhook/meta", content=tampered, headers=headers)

    assert response.status_code == 403
    assert delivered == []


def test_a_signed_payload_that_is_not_an_object_is_not_dispatched(
    client: TestClient, messenger_on: None, delivered: list[dict[str, Any]]
) -> None:
    """Correctly signed, valid JSON, wrong shape.

    Either answer is defensible here -- 400 because it is unusable, or 200 to
    stop the retries -- so the status is not what is asserted. What matters is
    that a body which passes verification but is not an envelope neither
    reaches the processor nor takes the route down with a 500.
    """
    body = _body(["not", "an", "envelope"])

    response = client.post("/webhook/meta", content=body, headers=_signed(body))

    assert response.status_code != 500
    assert delivered == []


def test_the_same_delivery_is_acknowledged_every_time_it_arrives(
    client: TestClient, messenger_on: None, delivered: list[dict[str, Any]]
) -> None:
    """Meta redelivers, and the edge must not try to be clever about it.

    De-duplication belongs to the worker, where the inbound claim makes a
    replay a no-op. Refusing or dropping a repeat at the route would earn
    hours of further retries for a message that was already handled -- and
    would break the one case redelivery exists for, where the first attempt
    was acknowledged but never processed.
    """
    body = _body(PAGE_DELIVERY)

    for _ in range(2):
        response = client.post("/webhook/meta", content=body, headers=_signed(body))
        assert response.status_code == 200

    assert delivered == [PAGE_DELIVERY, PAGE_DELIVERY]
