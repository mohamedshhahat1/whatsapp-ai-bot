"""Interactive menus: fixed selection ids, WhatsApp caps, payload shapes.

These tests build payloads without a real ``WhatsAppClient``. ``send_buttons``
touches nothing on ``self`` except ``_post``, so an uninitialised instance
with ``_post`` replaced exercises the whole of the code under test while
needing no Settings, no httpx client and no event-loop-bound resources to
tear down.

List menus are no longer sent, so there is nothing here that builds one. The
inbound side is a different question and is still covered: a row tapped on a
menu delivered before that change still arrives as a ``list_reply`` envelope
and must still route.
"""

import pytest

from app.integrations.whatsapp import (
    BUTTON_TITLE_MAX,
    MAX_BUTTONS,
    WhatsAppClient,
)
from app.services import menu, persona
from app.services.webhook_processor import _interactive_selection


class Recorder:
    """Stands in for ``WhatsAppClient._post`` and keeps what it was given."""

    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def __call__(self, payload: dict) -> dict:
        self.payloads.append(payload)
        return {"messages": [{"id": "wamid.test"}]}


def make_client() -> tuple[WhatsAppClient, Recorder]:
    client = object.__new__(WhatsAppClient)
    recorder = Recorder()
    client._post = recorder  # type: ignore[method-assign]
    return client, recorder


# --- The ids themselves -----------------------------------------------------


def test_selection_ids_are_pinned():
    """Ids are a contract with menus already in customers' chat histories.

    Renaming one does not migrate the messages we have already sent, so a tap
    on an old menu would arrive carrying an id nothing routes on. This test
    exists so that cost is paid deliberately rather than discovered later.
    """
    assert menu.SERVICE_FINISHING == "service_finishing"
    assert menu.SERVICE_COMMERCIAL == "service_commercial"
    assert menu.SERVICE_CONTRACTING == "service_contracting"
    assert menu.VIEW_PORTFOLIO == "view_portfolio"
    assert menu.REQUEST_QUOTE == "request_quote"
    assert menu.REQUEST_VISIT == "request_visit"
    assert menu.REQUEST_CALLBACK == "request_callback"
    assert menu.TALK_TO_EMPLOYEE == "talk_to_employee"


def test_every_id_has_a_label_and_is_routable():
    ids = [selection_id for selection_id, _ in [*menu._SERVICES, *menu._REQUESTS]]
    assert set(ids) == set(menu.KNOWN_SELECTIONS)
    assert len(ids) == len(set(ids)), "duplicate selection id"
    for selection_id in ids:
        assert menu.LABELS[selection_id].strip()


def test_routing_sets_do_not_overlap():
    """Overlap would make the branch order in ChatService load-bearing."""
    assert not (menu.SERVICE_SELECTIONS & menu.SALES_SELECTIONS)
    assert menu.TALK_TO_EMPLOYEE not in menu.SERVICE_SELECTIONS
    assert menu.TALK_TO_EMPLOYEE not in menu.SALES_SELECTIONS
    assert menu.SERVICE_SELECTIONS <= menu.KNOWN_SELECTIONS
    assert menu.SALES_SELECTIONS <= menu.KNOWN_SELECTIONS


def test_buttons_fit_whatsapp_caps():
    """A cap breach is a 400 in production and nothing at all in development."""
    assert len(menu.NEXT_STEP_BUTTONS) <= MAX_BUTTONS
    for selection_id, title in menu.NEXT_STEP_BUTTONS:
        assert selection_id in menu.KNOWN_SELECTIONS
        assert len(title) <= BUTTON_TITLE_MAX


def test_service_ack_names_the_choice_not_the_id():
    ack = menu.service_ack(menu.SERVICE_FINISHING)
    assert menu.LABELS[menu.SERVICE_FINISHING] in ack
    assert "service_finishing" not in ack


# --- Reply buttons ----------------------------------------------------------


async def test_send_buttons_payload_shape():
    client, recorder = make_client()
    await client.send_buttons(
        "20100", "body text", [("a_id", "A"), ("b_id", "B")], footer="footer"
    )
    payload = recorder.payloads[0]
    assert payload["type"] == "interactive"
    assert payload["to"] == "20100"
    interactive = payload["interactive"]
    assert interactive["type"] == "button"
    assert interactive["body"]["text"] == "body text"
    assert interactive["footer"]["text"] == "footer"
    buttons = interactive["action"]["buttons"]
    assert [b["reply"]["id"] for b in buttons] == ["a_id", "b_id"]
    assert all(b["type"] == "reply" for b in buttons)


async def test_send_buttons_omits_absent_footer():
    client, recorder = make_client()
    await client.send_buttons("20100", "body", [("a_id", "A")])
    assert "footer" not in recorder.payloads[0]["interactive"]


async def test_send_buttons_clamps_count_and_title():
    client, recorder = make_client()
    await client.send_buttons(
        "20100",
        "body",
        [(f"id_{i}", "T" * 40) for i in range(5)],
    )
    buttons = recorder.payloads[0]["interactive"]["action"]["buttons"]
    assert len(buttons) == MAX_BUTTONS
    assert all(len(b["reply"]["title"]) == BUTTON_TITLE_MAX for b in buttons)


async def test_send_buttons_rejects_empty():
    client, _ = make_client()
    with pytest.raises(ValueError):
        await client.send_buttons("20100", "body", [])


async def test_the_real_buttons_send():
    """The shipped buttons must survive the client's own validation."""
    client, recorder = make_client()
    await client.send_buttons("20100", "body", menu.NEXT_STEP_BUTTONS)
    buttons = recorder.payloads[0]["interactive"]["action"]["buttons"]
    sent = [b["reply"]["id"] for b in buttons]
    assert sent == [selection_id for selection_id, _ in menu.NEXT_STEP_BUTTONS]


def test_nothing_sends_list_messages_any_more():
    """Regression: the list path was removed, ids and routing were not.

    Re-adding a send_list here would quietly bring back a second, divergent
    definition of the menu -- the reason this was removed in the first place.

    ``WELCOME_MENU_BODY`` belongs in this list for the same reason. It was the
    body of the welcome when the options arrived as tappable rows, so it
    deliberately did not spell them out as bullets. With no list to carry it,
    it was a second definition of the welcome that nothing sent, and it
    survived the removal of the list path by nine months of nobody noticing.
    """
    assert not hasattr(WhatsAppClient, "send_list")
    assert not hasattr(menu, "MENU_SECTIONS")
    assert not hasattr(menu, "MENU_BUTTON")
    assert not hasattr(persona, "WELCOME_MENU_BODY")


# --- Inbound envelopes ------------------------------------------------------


def test_button_reply_envelope():
    selection = _interactive_selection(
        {
            "type": "interactive",
            "interactive": {
                "type": "button_reply",
                "button_reply": {"id": "request_quote", "title": "whatever"},
            },
        }
    )
    assert selection == ("request_quote", "whatever")


def test_list_reply_envelope():
    """We no longer send list menus; the delivered ones are still tappable."""
    selection = _interactive_selection(
        {
            "type": "interactive",
            "interactive": {
                "type": "list_reply",
                "list_reply": {
                    "id": "service_finishing",
                    "title": "whatever",
                    "description": "",
                },
            },
        }
    )
    assert selection == ("service_finishing", "whatever")


def test_missing_title_still_yields_the_id():
    """The id is what routes; a missing label must not lose the selection."""
    selection = _interactive_selection(
        {"interactive": {"type": "button_reply", "button_reply": {"id": "x_id"}}}
    )
    assert selection == ("x_id", "")


@pytest.mark.parametrize(
    "message",
    [
        {},
        {"interactive": {}},
        {"interactive": {"type": "button_reply", "button_reply": {}}},
        {"interactive": {"type": "button_reply", "button_reply": {"id": ""}}},
        {"interactive": "not-a-dict"},
    ],
)
def test_unroutable_envelopes_return_none(message):
    assert _interactive_selection(message) is None
