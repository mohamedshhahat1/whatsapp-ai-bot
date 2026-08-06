"""Messenger adapter and Meta dispatch.

Weighted deliberately towards parsing. The outbound half is a thin wrapper
over one HTTP call and fails loudly; the inbound half is where Messenger
differs from WhatsApp in ways that fail SILENTLY:

* the page's own messages come back as webhooks, and answering one produces a
  reply that also echoes -- an OpenAI charge per turn until somebody notices
* delivery and read receipts arrive in the same array as real messages
* a tapped quick reply carries its routing id in a payload beside the visible
  label, and reading the label works right up until the copy is translated

None of those raise. Each is a test here.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from app.channels.config import ChannelSettings
from app.channels.constants import MESSENGER
from app.channels.events import (
    EVENT_MEDIA,
    EVENT_SELECTION,
    EVENT_TEXT,
    EVENT_UNSUPPORTED,
    InboundEvent,
)
from app.channels.messenger import (
    MAX_QUICK_REPLIES,
    QUICK_REPLY_TITLE_MAX,
    TEXT_MAX,
    MessengerAdapter,
)
from app.core.inbound_config import InboundSettings
from app.services import webhook_processor

PAGE_ID = "100000000000001"
CUSTOMER = "7654321098765432"


def _settings(**overrides: Any) -> ChannelSettings:
    """Channel settings that ignore whatever .env happens to exist."""
    return ChannelSettings(
        _env_file=None,
        facebook_page_id=PAGE_ID,
        facebook_page_access_token="test-token",
        **overrides,
    )


def _adapter(**overrides: Any) -> MessengerAdapter:
    return MessengerAdapter(_settings(**overrides))


def _delivery(*messaging: dict[str, Any]) -> dict[str, Any]:
    """One webhook delivery carrying the given messaging items."""
    return {
        "object": "page",
        "entry": [{"id": PAGE_ID, "time": 1730000000000, "messaging": list(messaging)}],
    }


def _text_item(text: str = "hello", mid: str = "m_1") -> dict[str, Any]:
    return {
        "sender": {"id": CUSTOMER},
        "recipient": {"id": PAGE_ID},
        "timestamp": 1730000000000,
        "message": {"mid": mid, "text": text},
    }


# --- Parsing: the things that fail silently ---------------------------------


def test_echo_of_our_own_message_is_dropped() -> None:
    """The loop this prevents costs a completion per turn."""
    adapter = _adapter()
    payload = _delivery(
        {
            "sender": {"id": PAGE_ID},
            "recipient": {"id": CUSTOMER},
            "timestamp": 1730000000000,
            "message": {"mid": "m_echo", "text": "our reply", "is_echo": True},
        }
    )
    assert list(adapter.parse(payload)) == []


def test_message_from_the_page_id_is_dropped_even_without_the_echo_flag() -> None:
    adapter = _adapter()
    payload = _delivery(
        {
            "sender": {"id": PAGE_ID},
            "recipient": {"id": CUSTOMER},
            "timestamp": 1730000000000,
            "message": {"mid": "m_echo", "text": "our reply"},
        }
    )
    assert list(adapter.parse(payload)) == []


def test_delivery_receipt_is_dropped() -> None:
    """Falling through to 'unsupported' would apologise to a customer who
    did nothing at all."""
    adapter = _adapter()
    payload = _delivery(
        {
            "sender": {"id": CUSTOMER},
            "recipient": {"id": PAGE_ID},
            "timestamp": 1730000000000,
            "delivery": {"mids": ["m_1"], "watermark": 1730000000000},
        }
    )
    assert list(adapter.parse(payload)) == []


def test_read_receipt_is_dropped() -> None:
    adapter = _adapter()
    payload = _delivery(
        {
            "sender": {"id": CUSTOMER},
            "recipient": {"id": PAGE_ID},
            "timestamp": 1730000000000,
            "read": {"watermark": 1730000000000},
        }
    )
    assert list(adapter.parse(payload)) == []


def test_quick_reply_routes_on_payload_not_on_the_visible_label() -> None:
    """The label is copy and will be translated; the payload is the id."""
    adapter = _adapter()
    payload = _delivery(
        {
            "sender": {"id": CUSTOMER},
            "recipient": {"id": PAGE_ID},
            "timestamp": 1730000000000,
            "message": {
                "mid": "m_qr",
                "text": "\u0637\u0644\u0628 \u0639\u0631\u0636 \u0633\u0639\u0631",
                "quick_reply": {"payload": "request_quote"},
            },
        }
    )
    (event,) = list(adapter.parse(payload))
    assert event.kind == EVENT_SELECTION
    assert event.selection_id == "request_quote"
    assert event.selection_title == (
        "\u0637\u0644\u0628 \u0639\u0631\u0636 \u0633\u0639\u0631"
    )


def test_postback_becomes_a_selection() -> None:
    adapter = _adapter()
    payload = _delivery(
        {
            "sender": {"id": CUSTOMER},
            "recipient": {"id": PAGE_ID},
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


def test_attachment_becomes_media_and_keeps_the_caption() -> None:
    adapter = _adapter()
    payload = _delivery(
        {
            "sender": {"id": CUSTOMER},
            "recipient": {"id": PAGE_ID},
            "timestamp": 1730000000000,
            "message": {
                "mid": "m_img",
                "text": "look at this",
                "attachments": [
                    {"type": "image", "payload": {"url": "https://cdn/x.jpg"}}
                ],
            },
        }
    )
    (event,) = list(adapter.parse(payload))
    assert event.kind == EVENT_MEDIA
    assert event.media_type == "image"
    assert event.media_url == "https://cdn/x.jpg"
    assert event.caption == "look at this"


def test_plain_text_becomes_a_text_event() -> None:
    adapter = _adapter()
    (event,) = list(adapter.parse(_delivery(_text_item("\u0639\u0627\u064a\u0632"))))
    assert event.kind == EVENT_TEXT
    assert event.channel == MESSENGER
    assert event.sender_id == CUSTOMER
    assert event.text == "\u0639\u0627\u064a\u0632"


def test_several_customers_in_one_delivery_are_all_parsed() -> None:
    adapter = _adapter()
    payload = _delivery(
        _text_item("first", mid="m_a"),
        {
            "sender": {"id": "OTHER"},
            "recipient": {"id": PAGE_ID},
            "timestamp": 1730000000000,
            "message": {"mid": "m_b", "text": "second"},
        },
    )
    events = list(adapter.parse(payload))
    assert [e.sender_id for e in events] == [CUSTOMER, "OTHER"]


def test_timestamp_is_read_as_milliseconds() -> None:
    """Seconds would put every message ~55 years in the past, and the
    freshness gate would then refuse to answer anything."""
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


# --- Outbound payload shapes ------------------------------------------------


async def test_send_text_wraps_the_recipient_and_clamps_length() -> None:
    adapter = _adapter()
    captured: dict[str, Any] = {}

    async def fake_post(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {"message_id": "m_out"}

    adapter._post = fake_post  # type: ignore[method-assign]
    await adapter.send_text(CUSTOMER, "x" * (TEXT_MAX + 50))

    assert captured["recipient"] == {"id": CUSTOMER}
    assert captured["messaging_type"] == "RESPONSE"
    assert len(captured["message"]["text"]) == TEXT_MAX
    await adapter.aclose()


async def test_quick_replies_carry_the_selection_id_as_payload() -> None:
    adapter = _adapter()
    captured: dict[str, Any] = {}

    async def fake_post(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {"message_id": "m_out"}

    adapter._post = fake_post  # type: ignore[method-assign]
    await adapter.send_quick_replies(
        CUSTOMER, "pick one", [("request_quote", "A title far longer than allowed")]
    )

    (reply,) = captured["message"]["quick_replies"]
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


# --- Dispatch ---------------------------------------------------------------


class _RecordingService:
    """Stands in for ChatService and records which handler was chosen."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def handle_text_message(self, *args: Any) -> None:
        self.calls.append(("text", args))

    async def handle_interactive_message(self, *args: Any) -> None:
        self.calls.append(("interactive", args))

    async def handle_media_message(self, *args: Any) -> None:
        self.calls.append(("media", args))

    async def handle_unsupported_message(self, *args: Any) -> None:
        self.calls.append(("unsupported", args))


def _event(**overrides: Any) -> InboundEvent:
    defaults: dict[str, Any] = {
        "channel": MESSENGER,
        "sender_id": CUSTOMER,
        "provider_message_id": "m_1",
        "kind": EVENT_TEXT,
    }
    defaults.update(overrides)
    return InboundEvent(**defaults)


async def test_text_event_reaches_the_text_handler() -> None:
    service = _RecordingService()
    await webhook_processor._dispatch_event(service, _event(text="hi"))
    assert service.calls[0][0] == "text"
    assert service.calls[0][1] == (CUSTOMER, None, "m_1", "hi")


async def test_selection_event_reaches_the_interactive_handler() -> None:
    service = _RecordingService()
    await webhook_processor._dispatch_event(
        service,
        _event(kind=EVENT_SELECTION, selection_id="request_quote", selection_title="Q"),
    )
    assert service.calls[0][0] == "interactive"
    assert service.calls[0][1] == (CUSTOMER, None, "m_1", "request_quote", "Q")


async def test_selection_without_a_payload_is_treated_as_unsupported() -> None:
    """Better than guessing the route from the visible label."""
    service = _RecordingService()
    await webhook_processor._dispatch_event(
        service, _event(kind=EVENT_SELECTION, selection_id="", selection_title="Q")
    )
    assert service.calls[0][0] == "unsupported"


async def test_media_event_reaches_the_media_handler() -> None:
    service = _RecordingService()
    await webhook_processor._dispatch_event(
        service, _event(kind=EVENT_MEDIA, media_type="image", caption="c")
    )
    assert service.calls[0][0] == "media"
    assert service.calls[0][1] == (CUSTOMER, None, "m_1", "image", None, "c")


async def test_unsupported_event_reaches_the_unsupported_handler() -> None:
    service = _RecordingService()
    await webhook_processor._dispatch_event(service, _event(kind=EVENT_UNSUPPORTED))
    assert service.calls[0][0] == "unsupported"


async def test_event_without_a_message_id_is_dropped() -> None:
    """That id keys the inbound claim, the generation cache and the outbound
    reservation. Processing one without it would collide every such event on
    the empty string."""
    service = _RecordingService()
    await webhook_processor._dispatch_event(service, _event(provider_message_id=""))
    assert service.calls == []


async def test_event_without_a_sender_is_dropped() -> None:
    service = _RecordingService()
    await webhook_processor._dispatch_event(service, _event(sender_id=""))
    assert service.calls == []


# --- Freshness --------------------------------------------------------------


def _fixed_inbound(**overrides: Any) -> InboundSettings:
    return InboundSettings(_env_file=None, **overrides)


def test_a_recent_event_is_not_stale(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        webhook_processor, "get_inbound_settings", lambda: _fixed_inbound()
    )
    event = _event(sent_at=datetime.now(UTC) - timedelta(seconds=5))
    assert webhook_processor._event_is_stale(event) is False


def test_an_old_event_is_stale(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        webhook_processor,
        "get_inbound_settings",
        lambda: _fixed_inbound(inbound_max_age_minutes=10),
    )
    event = _event(sent_at=datetime.now(UTC) - timedelta(hours=3))
    assert webhook_processor._event_is_stale(event) is True


def test_an_event_with_no_timestamp_fails_open(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        webhook_processor, "get_inbound_settings", lambda: _fixed_inbound()
    )
    assert webhook_processor._event_is_stale(_event(sent_at=None)) is False


def test_staleness_is_not_enforced_when_switched_off(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        webhook_processor,
        "get_inbound_settings",
        lambda: _fixed_inbound(reject_stale_inbound=False),
    )
    event = _event(sent_at=datetime.now(UTC) - timedelta(days=2))
    assert webhook_processor._event_is_stale(event) is False
