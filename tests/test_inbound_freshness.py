"""The inbound freshness gate: what may be answered, and what may not.

These cover the regression behind an unprompted welcome. A message Meta
redelivered long after it was sent must not be treated as a live conversation
opening, because past the reopen window that mints a new session and a new
session is owed a greeting.

Most of this needs no database: the gate is a decision about a payload, and it
is asserted by watching whether a handler would have been reached at all.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.core.inbound_config import InboundSettings
from app.services import webhook_processor
from app.services.webhook_processor import (
    _content_of,
    _message_age,
    process_webhook_payload,
)


def ts(**delta: float) -> str:
    """A WhatsApp timestamp this long ago, in Meta's format (unix seconds)."""
    moment = datetime.now(UTC) - timedelta(**delta)
    return str(int(moment.timestamp()))


def text_payload(body: str = "\\u0645\\u0631\\u062d\\u0628\\u0627", **kwargs) -> dict:
    """One webhook delivery carrying one text message.

    Defaults to an Arabic greeting because that is the input that takes the
    greeting-only branch -- the one that sends the welcome AND the menu, which
    is exactly what was seen arriving unprompted.
    """
    message: dict = {
        "from": "20100000000",
        "id": "wamid.test",
        "type": "text",
        "text": {"body": body},
    }
    message.update(kwargs)
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [
                                {
                                    "wa_id": "20100000000",
                                    "profile": {"name": "Test"},
                                }
                            ],
                            "messages": [message],
                        }
                    }
                ]
            }
        ]
    }


class FakeService:
    """Stands in for ChatService and records what it was asked to do."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.statuses: list[tuple[str, str]] = []

    async def handle_text_message(self, wa_id, name, wa_message_id, text) -> None:
        self.texts.append(text)

    async def handle_status_update(self, wa_message_id, status) -> None:
        self.statuses.append((wa_message_id, status))


@pytest.fixture
def routed(monkeypatch):
    """Run the payload processor with the real gate and a fake handler.

    ``record_without_answering`` is replaced too, so the stale path needs no
    database: what is asserted is that it was chosen, not what it stored.
    """
    service = FakeService()
    recorded: list[dict] = []

    monkeypatch.setattr(
        webhook_processor, "ChatService", lambda *args, **kwargs: service
    )

    async def fake_record(session, settings, **kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(webhook_processor, "record_without_answering", fake_record)

    def run(payload: dict, **overrides):
        settings = InboundSettings(**overrides)
        monkeypatch.setattr(webhook_processor, "get_inbound_settings", lambda: settings)
        return payload, service, recorded

    return run


async def _process(payload):
    await process_webhook_payload(None, None, None, None, payload)


# --- the gate ------------------------------------------------------------


async def test_a_fresh_message_is_answered_normally(routed):
    payload, service, recorded = routed(text_payload(timestamp=ts(seconds=5)))
    await _process(payload)
    assert service.texts == ["\\u0645\\u0631\\u062d\\u0628\\u0627"]
    assert recorded == []


async def test_a_stale_message_never_reaches_a_handler(routed):
    # 40 minutes: the reported symptom, and past the 30-minute reopen window,
    # so the live path would have opened a new session and greeted it.
    payload, service, recorded = routed(text_payload(timestamp=ts(minutes=40)))
    await _process(payload)
    assert service.texts == []
    assert len(recorded) == 1
    assert recorded[0]["wa_message_id"] == "wamid.test"
    assert recorded[0]["age"] > timedelta(minutes=39)


async def test_a_message_just_inside_the_bound_is_answered(routed):
    payload, service, recorded = routed(text_payload(timestamp=ts(minutes=9)))
    await _process(payload)
    assert service.texts and recorded == []


async def test_the_bound_is_configurable_not_hardcoded(routed):
    # The same delivery that was stale above is live under a wider bound.
    payload, service, recorded = routed(
        text_payload(timestamp=ts(minutes=40)), inbound_max_age_minutes=120
    )
    await _process(payload)
    assert service.texts and recorded == []


async def test_the_gate_can_be_switched_off(routed):
    payload, service, recorded = routed(
        text_payload(timestamp=ts(days=3)), reject_stale_inbound=False
    )
    await _process(payload)
    assert service.texts and recorded == []


async def test_zero_max_age_disables_the_gate_like_the_switch(routed):
    payload, service, recorded = routed(
        text_payload(timestamp=ts(days=3)), inbound_max_age_minutes=0
    )
    await _process(payload)
    assert service.texts and recorded == []


# --- failing open --------------------------------------------------------


async def test_a_missing_timestamp_is_treated_as_fresh(routed):
    """Silencing every reply would be far worse than the bug being fixed."""
    payload, service, recorded = routed(text_payload())
    await _process(payload)
    assert service.texts and recorded == []


async def test_an_unparseable_timestamp_is_treated_as_fresh(routed):
    payload, service, recorded = routed(text_payload(timestamp="not-a-number"))
    await _process(payload)
    assert service.texts and recorded == []


async def test_a_message_with_no_id_is_left_to_the_normal_path(routed):
    """There is nothing to key a claim on, so it must not be swallowed here."""
    payload = text_payload(timestamp=ts(minutes=40))
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"] = ""
    payload, service, recorded = routed(payload)
    await _process(payload)
    assert recorded == []
    assert service.texts


# --- neighbouring behaviour that must not regress ------------------------


async def test_status_updates_are_unaffected_by_the_gate(routed):
    """A delivery receipt is not a message and has no age check."""
    payload, service, recorded = routed(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [{"id": "wamid.out", "status": "delivered"}]
                            }
                        }
                    ]
                }
            ]
        }
    )
    await _process(payload)
    assert service.statuses == [("wamid.out", "delivered")]
    assert recorded == []


# --- helpers -------------------------------------------------------------


def test_message_age_reads_metas_unix_seconds():
    age = _message_age({"timestamp": ts(minutes=10)})
    assert age is not None
    assert timedelta(minutes=9) < age < timedelta(minutes=11)


def test_message_age_is_none_when_it_cannot_be_read():
    assert _message_age({}) is None
    assert _message_age({"timestamp": None}) is None
    assert _message_age({"timestamp": ""}) is None
    assert _message_age({"timestamp": {"nested": 1}}) is None


def test_stored_content_matches_what_the_live_handler_would_store():
    assert _content_of({"type": "text", "text": {"body": "hello"}}) == "hello"
    assert _content_of({"type": "image", "image": {}}) == "[image received]"
    assert _content_of({"type": "document", "document": {"caption": "c"}}) == "c"
    assert _content_of({"type": "audio"}) == "[audio received]"
    interactive = {
        "type": "interactive",
        "interactive": {
            "list_reply": {"id": "service_finishing", "title": "Finishing"}
        },
    }
    assert _content_of(interactive) == "Finishing"


def test_default_max_age_stays_below_the_reopen_window():
    """Above the reopen window the gate stops covering the dangerous zone.

    Past that window a late delivery mints a NEW session, and a new session is
    owed a welcome. Below it, a late delivery lands back in the session it
    belongs to, where answering it is harmless.
    """
    from app.config import get_settings

    assert InboundSettings().inbound_max_age < get_settings().conversation_reopen_window
