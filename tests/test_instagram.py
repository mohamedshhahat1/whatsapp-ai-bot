"""Instagram DM adapter.

Weighted towards the places Instagram differs from Messenger, because those
are the ones that fail SILENTLY rather than loudly:

* the account's own messages come back as webhooks on the same subscription as
  real ones -- answering one produces a reply that also echoes, an OpenAI
  charge per turn until somebody notices
* the text limit is expressed in BYTES, and Arabic is two bytes a letter, so a
  character-based clamp passes every English test and then sends a body twice
  the documented limit to a real customer
* read receipts and reactions arrive in the same array as messages
* a tapped quick reply carries its routing id in a payload beside the visible
  label, and reading the label works right up until the copy is translated

None of those raise. Each is a test here. No database is needed, so these run
everywhere instead of skipping on a machine without Postgres.

The Arabic below is written as Arabic rather than as \\uXXXX escapes, for the
reason app/services/persona.py gives: escaped codepoints cannot be proofread.
"""

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.channels.config import ChannelSettings
from app.channels.constants import INSTAGRAM_DM
from app.channels.events import (
    EVENT_MEDIA,
    EVENT_SELECTION,
    EVENT_TEXT,
    EVENT_UNSUPPORTED,
)
from app.channels.instagram import (
    GRAPH_API_BASE,
    MAX_QUICK_REPLIES,
    QUICK_REPLY_TITLE_MAX,
    SEND_PATH,
    TEXT_MAX_BYTES,
    InstagramDMAdapter,
    clip_utf8,
)
from app.core.exceptions import ExternalServiceError

# The app user's own Instagram professional account, as it arrives in entry.id.
IG_ACCOUNT = "17841400000000001"
# An Instagram-scoped id for the customer. Deliberately not phone-shaped.
CUSTOMER = "6789012345678901"

# One Arabic letter, two bytes in UTF-8. The whole point of the byte clamp.
ARABIC_LETTER = "أ"


def _settings(**overrides: Any) -> ChannelSettings:
    """Channel settings that ignore whatever .env happens to exist.

    The defaults are merged into a dict rather than passed beside
    ``**overrides``: a test that pins one of them -- an empty account id, an
    empty Instagram token -- would otherwise supply the same keyword twice and
    raise TypeError before reaching its first assertion.
    """
    values: dict[str, Any] = {
        "instagram_account_id": IG_ACCOUNT,
        "instagram_access_token": "unit-test-placeholder",
    }
    values.update(overrides)
    return ChannelSettings(_env_file=None, **values)


def _adapter(**overrides: Any) -> InstagramDMAdapter:
    return InstagramDMAdapter(_settings(**overrides))


def _delivery(*messaging: dict[str, Any]) -> dict[str, Any]:
    """One webhook delivery carrying the given messaging items.

    ``object`` is ``instagram`` rather than ``page``; that difference is what
    the route keys on and it is asserted in tests/test_meta_webhook_instagram.py.
    """
    return {
        "object": "instagram",
        "entry": [
            {
                "id": IG_ACCOUNT,
                "time": 1730000000000,
                "messaging": list(messaging),
            }
        ],
    }


def _text_item(text: str = "hello", mid: str = "m_1") -> dict[str, Any]:
    return {
        "sender": {"id": CUSTOMER},
        "recipient": {"id": IG_ACCOUNT},
        "timestamp": 1730000000000,
        "message": {"mid": mid, "text": text},
    }


# --- Parsing: the things that fail silently ---------------------------------


def test_echo_of_our_own_message_is_dropped() -> None:
    """Instagram delivers echoes on the same subscription as real messages."""
    adapter = _adapter()
    payload = _delivery(
        {
            "sender": {"id": IG_ACCOUNT},
            "recipient": {"id": CUSTOMER},
            "timestamp": 1730000000000,
            "message": {"mid": "m_echo", "text": "our reply", "is_echo": True},
        }
    )
    assert list(adapter.parse(payload)) == []


def test_self_message_is_dropped() -> None:
    """is_self marks a message the account sent to itself."""
    adapter = _adapter()
    item = _text_item()
    item["message"]["is_self"] = True
    assert list(adapter.parse(_delivery(item))) == []


def test_message_from_the_account_id_is_dropped_without_any_flag() -> None:
    """The last line of defence if Meta ever omits the flag."""
    adapter = _adapter()
    payload = _delivery(
        {
            "sender": {"id": IG_ACCOUNT},
            "recipient": {"id": CUSTOMER},
            "timestamp": 1730000000000,
            "message": {"mid": "m_echo", "text": "our reply"},
        }
    )
    assert list(adapter.parse(payload)) == []


def test_message_from_the_configured_account_id_is_dropped() -> None:
    """Covers a delivery whose entry.id is missing or differs from settings."""
    adapter = _adapter()
    payload = {
        "object": "instagram",
        "entry": [
            {
                "messaging": [
                    {
                        "sender": {"id": IG_ACCOUNT},
                        "recipient": {"id": CUSTOMER},
                        "timestamp": 1730000000000,
                        "message": {"mid": "m_echo", "text": "ours"},
                    }
                ]
            }
        ],
    }
    assert list(adapter.parse(payload)) == []


def test_a_customer_is_not_dropped_when_no_account_id_is_configured() -> None:
    """An empty setting must not turn into a guard that matches everyone."""
    adapter = _adapter(instagram_account_id="")
    payload = {
        "object": "instagram",
        "entry": [{"messaging": [_text_item("still a customer")]}],
    }
    (event,) = list(adapter.parse(payload))
    assert event.sender_id == CUSTOMER


def test_read_receipt_is_dropped() -> None:
    adapter = _adapter()
    payload = _delivery(
        {
            "sender": {"id": CUSTOMER},
            "recipient": {"id": IG_ACCOUNT},
            "timestamp": 1730000000000,
            "read": {"mid": "m_1"},
        }
    )
    assert list(adapter.parse(payload)) == []


def test_reaction_is_dropped() -> None:
    """A heart on a message is not a question."""
    adapter = _adapter()
    payload = _delivery(
        {
            "sender": {"id": CUSTOMER},
            "recipient": {"id": IG_ACCOUNT},
            "timestamp": 1730000000000,
            "reaction": {"mid": "m_1", "action": "react", "reaction": "love"},
        }
    )
    assert list(adapter.parse(payload)) == []


def test_unsent_message_is_dropped() -> None:
    """Answering a withdrawn message quotes something the customer removed."""
    adapter = _adapter()
    item = _text_item()
    item["message"]["is_deleted"] = True
    assert list(adapter.parse(_delivery(item))) == []


def test_unsupported_message_is_reported_as_unsupported() -> None:
    """Explicitly unsupported, not silently empty."""
    adapter = _adapter()
    item = _text_item()
    item["message"]["is_unsupported"] = True
    (event,) = list(adapter.parse(_delivery(item)))
    assert event.kind == EVENT_UNSUPPORTED


def test_quick_reply_routes_on_payload_not_on_the_visible_label() -> None:
    """The label is copy and will be translated; the payload is the id."""
    adapter = _adapter()
    label = "طلب عرض سعر"
    item = _text_item(label, mid="m_qr")
    item["message"]["quick_reply"] = {"payload": "request_quote"}
    (event,) = list(adapter.parse(_delivery(item)))
    assert event.kind == EVENT_SELECTION
    assert event.selection_id == "request_quote"
    assert event.selection_title == label


def test_postback_becomes_a_selection() -> None:
    adapter = _adapter()
    payload = _delivery(
        {
            "sender": {"id": CUSTOMER},
            "recipient": {"id": IG_ACCOUNT},
            "timestamp": 1730000000000,
            "postback": {
                "mid": "m_pb",
                "payload": "talk_to_employee",
                "title": "Talk to us",
            },
        }
    )
    (event,) = list(adapter.parse(payload))
    assert event.kind == EVENT_SELECTION
    assert event.selection_id == "talk_to_employee"
    assert event.provider_message_id == "m_pb"


def test_story_mention_becomes_media() -> None:
    """An Instagram-specific attachment type the shared pipeline sees as media."""
    adapter = _adapter()
    item = _text_item(mid="m_story")
    item["message"] = {
        "mid": "m_story",
        "attachments": [
            {"type": "story_mention", "payload": {"url": "https://cdn/story.jpg"}}
        ],
    }
    (event,) = list(adapter.parse(_delivery(item)))
    assert event.kind == EVENT_MEDIA
    assert event.media_type == "story_mention"
    assert event.media_url == "https://cdn/story.jpg"


def test_attachment_keeps_the_caption() -> None:
    adapter = _adapter()
    item = _text_item(mid="m_img")
    item["message"] = {
        "mid": "m_img",
        "text": "look at this",
        "attachments": [{"type": "image", "payload": {"url": "https://cdn/x.jpg"}}],
    }
    (event,) = list(adapter.parse(_delivery(item)))
    assert event.kind == EVENT_MEDIA
    assert event.caption == "look at this"


def test_plain_text_becomes_a_text_event_on_the_instagram_channel() -> None:
    adapter = _adapter()
    body = "عايز"
    (event,) = list(adapter.parse(_delivery(_text_item(body))))
    assert event.kind == EVENT_TEXT
    assert event.channel == INSTAGRAM_DM
    assert event.sender_id == CUSTOMER
    assert event.text == body


def test_several_customers_in_one_delivery_are_all_parsed() -> None:
    adapter = _adapter()
    other = _text_item("second", mid="m_b")
    other["sender"] = {"id": "OTHER"}
    events = list(adapter.parse(_delivery(_text_item("first", mid="m_a"), other)))
    assert [e.sender_id for e in events] == [CUSTOMER, "OTHER"]


def test_timestamp_is_read_as_milliseconds() -> None:
    """Seconds would put every message ~55 years in the past, and the freshness
    gate would then refuse to answer anything."""
    adapter = _adapter()
    (event,) = list(adapter.parse(_delivery(_text_item())))
    assert event.sent_at is not None
    expected = datetime.fromtimestamp(1730000000, tz=UTC)
    assert abs((event.sent_at - expected).total_seconds()) < 1


def test_unreadable_timestamp_fails_open() -> None:
    """A format change on Meta's side must not silence every reply."""
    adapter = _adapter()
    item = _text_item()
    item["timestamp"] = "not-a-number"
    (event,) = list(adapter.parse(_delivery(item)))
    assert event.sent_at is None
    assert event.age is None


# --- The byte limit ---------------------------------------------------------


def test_clip_utf8_leaves_short_text_alone() -> None:
    assert clip_utf8("hello") == "hello"


def test_the_arabic_fixture_really_is_two_bytes() -> None:
    """Guards the guard.

    Every byte-versus-character assertion below is meaningless if this letter
    is one ASCII byte, and an escaping accident in this file once made exactly
    that true. One line here keeps the rest honest.
    """
    assert len(ARABIC_LETTER) == 1
    assert len(ARABIC_LETTER.encode("utf-8")) == 2


def test_clip_utf8_measures_bytes_not_characters() -> None:
    """600 Arabic letters is 1,200 bytes: over the limit at half the length."""
    clipped = clip_utf8(ARABIC_LETTER * 600)
    assert len(clipped.encode("utf-8")) <= TEXT_MAX_BYTES
    assert len(clipped) == TEXT_MAX_BYTES // 2


def test_clip_utf8_never_splits_a_character() -> None:
    """An odd budget must not leave half a two-byte letter behind."""
    clipped = clip_utf8(ARABIC_LETTER * 10, max_bytes=5)
    assert clipped == ARABIC_LETTER * 2
    clipped.encode("utf-8").decode("utf-8")


# --- Outbound payload shapes ------------------------------------------------


def test_the_send_path_is_the_documented_one() -> None:
    """Pinned as a constant: /me/messages needs no id in the path, which is why
    the registry does not require facebook_page_id for this channel."""
    assert SEND_PATH == "/me/messages"


async def test_the_client_targets_the_pinned_graph_version() -> None:
    adapter = _adapter()
    assert str(adapter._client.base_url).startswith(GRAPH_API_BASE)
    assert "v21.0" in str(adapter._client.base_url)
    await adapter.aclose()


async def test_the_token_falls_back_to_the_page_token() -> None:
    """The documented normal setup: an IG account behind a page uses its token."""
    adapter = InstagramDMAdapter(
        _settings(instagram_access_token="", facebook_page_access_token="page-token")
    )
    assert adapter._client.headers["Authorization"] == "Bearer page-token"
    await adapter.aclose()


async def test_send_text_wraps_the_recipient_and_clamps_on_bytes() -> None:
    adapter = _adapter()
    captured: dict[str, Any] = {}

    async def fake_post(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {"recipient_id": CUSTOMER, "message_id": "m_out"}

    adapter._post = fake_post  # type: ignore[method-assign]
    await adapter.send_text(CUSTOMER, ARABIC_LETTER * 900)

    assert captured["recipient"] == {"id": CUSTOMER}
    assert captured["messaging_type"] == "RESPONSE"
    sent = captured["message"]["text"]
    assert len(sent.encode("utf-8")) <= TEXT_MAX_BYTES
    await adapter.aclose()


async def test_quick_replies_carry_the_selection_id_as_payload() -> None:
    adapter = _adapter()
    captured: dict[str, Any] = {}

    async def fake_post(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {"message_id": "m_out"}

    adapter._post = fake_post  # type: ignore[method-assign]
    await adapter.send_quick_replies(
        CUSTOMER,
        "pick one",
        [("request_quote", "A title far longer than allowed")],
    )

    (reply,) = captured["message"]["quick_replies"]
    assert reply["content_type"] == "text"
    assert reply["payload"] == "request_quote"
    assert len(reply["title"]) == QUICK_REPLY_TITLE_MAX
    await adapter.aclose()


async def test_quick_replies_are_capped() -> None:
    adapter = _adapter()
    captured: dict[str, Any] = {}

    async def fake_post(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {"message_id": "m_out"}

    adapter._post = fake_post  # type: ignore[method-assign]
    options = [(f"id_{i}", f"t{i}") for i in range(MAX_QUICK_REPLIES + 5)]
    await adapter.send_quick_replies(CUSTOMER, "pick", options)

    assert len(captured["message"]["quick_replies"]) == MAX_QUICK_REPLIES
    await adapter.aclose()


async def test_quick_replies_with_no_options_degrade_to_text() -> None:
    adapter = _adapter()
    captured: dict[str, Any] = {}

    async def fake_post(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {"message_id": "m_out"}

    adapter._post = fake_post  # type: ignore[method-assign]
    await adapter.send_quick_replies(CUSTOMER, "body", [])

    assert "quick_replies" not in captured["message"]
    assert captured["message"]["text"] == "body"
    await adapter.aclose()


# --- Failure mapping --------------------------------------------------------


async def test_an_api_error_becomes_an_external_service_error() -> None:
    """_send_once upstream relies on every adapter failing the same way."""
    adapter = _adapter()
    request = httpx.Request("POST", GRAPH_API_BASE + "/v21.0" + SEND_PATH)
    response = httpx.Response(400, text="bad request", request=request)

    async def fail(payload: dict[str, Any]) -> dict[str, Any]:
        raise httpx.HTTPStatusError("400", request=request, response=response)

    adapter._send = fail  # type: ignore[method-assign]
    with pytest.raises(ExternalServiceError):
        await adapter.send_text(CUSTOMER, "hi")
    await adapter.aclose()


async def test_a_network_error_becomes_an_external_service_error() -> None:
    adapter = _adapter()

    async def fail(payload: dict[str, Any]) -> dict[str, Any]:
        raise httpx.ConnectError("no route to host")

    adapter._send = fail  # type: ignore[method-assign]
    with pytest.raises(ExternalServiceError):
        await adapter.send_text(CUSTOMER, "hi")
    await adapter.aclose()
