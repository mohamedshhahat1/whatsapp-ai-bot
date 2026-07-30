"""Tests for the pre-model scope check.

The false-positive tests are the important half. A missed off-topic question
costs a fraction of a cent; a refused customer is gone. Every case in
``test_real_questions_are_never_refused`` is a message a real customer sends,
and any of them returning OUT is a bug that loses business silently -- the
customer does not complain, they just stop replying.
"""

import pytest

from app.services import intent


@pytest.mark.parametrize(
    "text",
    [
        "\u0641\u064a\u0646 \u0641\u0631\u0648\u0639\u0643\u0645\u061f",
        "\u0639\u0646\u062f\u0643\u0645 \u0636\u0645\u0627\u0646 \u0639\u0644\u0649 \u0627\u0644\u0634\u063a\u0644\u061f",
        "\u0628\u062a\u0639\u0645\u0644\u0648\u0627 \u0645\u0639\u0627\u064a\u0646\u0629\u061f",
        "\u0639\u0627\u064a\u0632 \u0623\u0639\u0631\u0641 \u0645\u0634\u0627\u0631\u064a\u0639\u0643\u0645 \u0627\u0644\u0633\u0627\u0628\u0642\u0629",
        "what warranty do you offer?",
        "do you have a branch in Nasr City?",
        "can I get a quotation",
    ],
)
def test_company_questions(text: str) -> None:
    assert intent.classify(text) == intent.COMPANY


@pytest.mark.parametrize(
    "text",
    [
        "\u0625\u064a\u0647 \u0647\u0648 \u0627\u0644\u062c\u0628\u0633 \u0628\u0648\u0631\u062f\u061f",
        "\u0627\u0644\u062f\u0647\u0627\u0646 \u0628\u064a\u0627\u062e\u062f \u0642\u062f \u0625\u064a\u0647 \u0639\u0634\u0627\u0646 \u064a\u0646\u0634\u0641\u061f",
        "\u0627\u0644\u0633\u0628\u0627\u0643\u0629 \u0648\u0644\u0627 \u0627\u0644\u0645\u062d\u0627\u0631\u0629 \u0627\u0644\u0623\u0648\u0644\u061f",
        "\u0627\u0644\u0641\u0631\u0642 \u0628\u064a\u0646 \u0627\u0644\u0633\u064a\u0631\u0627\u0645\u064a\u0643 \u0648\u0627\u0644\u0628\u0648\u0631\u0633\u0644\u064a\u0646",
        "what is drywall made of",
        "does wiring come before plastering",
    ],
)
def test_general_finishing_questions(text: str) -> None:
    assert intent.classify(text) == intent.DOMAIN


@pytest.mark.parametrize(
    "text",
    [
        "who is the president of France?",
        "\u0645\u064a\u0646 \u0631\u0626\u064a\u0633 \u0641\u0631\u0646\u0633\u0627 \u062f\u0644\u0648\u0642\u062a\u064a\u061f",
        "what is the weather like tomorrow",
        "can you write me a poem about the sea",
        "\u0645\u064a\u0646 \u0643\u0633\u0628 \u0645\u0628\u0627\u0631\u0627\u0629 \u0627\u0644\u0623\u0647\u0644\u064a \u0625\u0645\u0628\u0627\u0631\u062d\u061f",
        "give me a recipe for chicken please",
        "what is the capital of Australia",
        "can you help me with my python homework",
    ],
)
def test_out_of_scope_questions(text: str) -> None:
    assert intent.classify(text) == intent.OUT


@pytest.mark.parametrize(
    "text",
    [
        # Follow-ups that only mean something against the previous turn.
        "\u0623\u064a\u0648\u0647",
        "\u062a\u0645\u0627\u0645",
        "\u0648\u0627\u0644\u0641\u064a\u0644\u0627\u061f",
        "\u0643\u0627\u0645 \u064a\u0648\u0645\u061f",
        "120",
        "ok",
        "yes please",
        "and after that?",
        # Ordinary business messages with no keyword at all.
        "\u0645\u0645\u0643\u0646 \u062d\u0636\u0631\u062a\u0643 \u062a\u0648\u0636\u062d \u0644\u064a \u0623\u0643\u062a\u0631\u061f",
        "I did not understand what you said",
        "\u0645\u0645\u0643\u0646 \u062a\u0628\u0639\u062a\u0644\u064a \u0635\u0648\u0631\u061f",
        # A president who is not a head of state.
        "\u0645\u064a\u0646 \u0631\u0626\u064a\u0633 \u0645\u062c\u0644\u0633 \u0625\u062f\u0627\u0631\u0629 \u0627\u0644\u0634\u0631\u0643\u0629\u061f",
    ],
)
def test_real_questions_are_never_refused(text: str) -> None:
    """The expensive failure. None of these may return OUT."""
    assert intent.classify(text) != intent.OUT


def test_empty_and_none_default_to_company() -> None:
    assert intent.classify(None) == intent.COMPANY
    assert intent.classify("") == intent.COMPANY
    assert intent.classify("   ") == intent.COMPANY


def test_domain_terms_beat_out_of_scope_terms() -> None:
    """A finishing question is never refused for containing a stray word."""
    assert (
        intent.classify("is the weather a problem for exterior paint drying")
        == intent.DOMAIN
    )


def test_refusal_names_the_company() -> None:
    reply = intent.out_of_scope_reply("Al-Kayan")
    assert "Al-Kayan" in reply


def test_refusal_works_without_a_configured_name() -> None:
    reply = intent.out_of_scope_reply("")
    assert "our services" in reply
    assert "{name}" not in reply


def test_refusal_mentions_finishing_and_contracting() -> None:
    """It redirects rather than only declining."""
    assert "finishing and contracting" in intent.out_of_scope_reply("Al-Kayan")
