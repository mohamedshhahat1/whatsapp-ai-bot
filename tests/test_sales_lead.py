"""Tests for sales-lead classification.

The interesting question is not whether ``is_sales_lead`` fires -- it is where
the line sits between a handoff and a *lead*. Both need a person. Only one
jumps the operator queue.

If everything is a lead, the top of the list is just the list, and operators
learn to ignore the badge. So the complaint cases below matter as much as the
lead cases.
"""

import pytest

from app.core.events import conversation_handoff
from app.models.conversation import TAG_SALES_LEAD
from app.services.handoff import is_sales_lead, wants_human


@pytest.mark.parametrize(
    "text",
    [
        # asking for the sales side by name
        "\u0639\u0627\u064a\u0632 \u0645\u062f\u064a\u0631 \u0627\u0644\u0645\u0628\u064a\u0639\u0627\u062a",
        "\u0645\u062f\u064a\u0631 \u0627\u0644\u0645\u0628\u064a\u0639\u0627\u062a \u0644\u0648 \u0633\u0645\u062d\u062a",
        "contact the sales manager",
        "I want to talk to the sales team",
        # asking to be contacted
        "\u0643\u0644\u0645\u0646\u064a",
        "\u0627\u062a\u0635\u0644 \u0628\u064a\u0627",
        "\u062a\u0648\u0627\u0635\u0644\u0648\u0627 \u0645\u0639\u0627\u064a\u0627",
        "\u0631\u0642\u0645\u064a 01000000000",
        "call me please",
        "can someone contact me",
        "please get back to me",
        # asking to speak to a person
        "I want to speak to someone",
        "\u0639\u0627\u064a\u0632 \u0623\u062a\u0643\u0644\u0645 \u0645\u0639 \u062d\u062f",
    ],
)
def test_these_are_leads(text: str) -> None:
    assert is_sales_lead(text) is True


@pytest.mark.parametrize(
    "text",
    [
        # A person is wanted, but nothing says money. A complaint routed into
        # the lead queue pushes an actual buyer down the list.
        "\u0645\u0648\u0638\u0641",
        "I want to speak to a manager about a complaint",
        "customer service",
        "\u062e\u062f\u0645\u0629 \u0627\u0644\u0639\u0645\u0644\u0627\u0621",
        "stop the bot",
        # Ordinary questions -- no handoff at all.
        "\u0641\u064a\u0646 \u0641\u0631\u0648\u0639\u0643\u0645\u061f",
        "what materials do you use?",
        "",
    ],
)
def test_these_are_not_leads(text: str) -> None:
    assert is_sales_lead(text) is False


def test_none_is_not_a_lead() -> None:
    assert is_sales_lead(None) is False


@pytest.mark.parametrize(
    "text",
    [
        "call me please",
        "I want to speak to someone",
        "\u0643\u0644\u0645\u0646\u064a",
        "contact the sales manager",
    ],
)
def test_leads_also_trigger_a_handoff(text: str) -> None:
    """Being a lead is useless if the bot keeps answering.

    ``is_sales_lead`` only chooses the tag; ``wants_human`` decides whether a
    person is involved at all. A phrase recognised by the first but not the
    second would tag nothing, because the tag is only ever applied on the
    handoff path.
    """
    assert wants_human(text) is True


def test_handoff_event_carries_the_tag() -> None:
    """The dashboard alerts on the event, before it refetches the row."""
    event = conversation_handoff(
        conversation_id=7,
        mode="human",
        assigned_operator=None,
        reason="customer_started_negotiating",
        tag=TAG_SALES_LEAD,
    )
    assert event["tag"] == TAG_SALES_LEAD
    assert event["conversation_id"] == 7


def test_handoff_event_tag_defaults_to_none() -> None:
    """An ordinary handoff must not look like a lead."""
    event = conversation_handoff(
        conversation_id=7,
        mode="human",
        assigned_operator=None,
        reason="customer_asked_for_a_human",
    )
    assert event["tag"] is None


def test_handoff_event_carries_no_customer_data() -> None:
    """The bus rule: no phone number, name or message body.

    Adding a field to this event is exactly when that rule gets broken, so it
    is asserted rather than left to review.
    """
    event = conversation_handoff(
        conversation_id=7,
        mode="human",
        assigned_operator="Sara",
        reason="customer_started_negotiating",
        tag=TAG_SALES_LEAD,
    )
    assert set(event) == {
        "type",
        "conversation_id",
        "mode",
        "assigned_operator",
        "reason",
        "tag",
        "at",
    }
