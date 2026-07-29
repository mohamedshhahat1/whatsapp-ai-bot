"""Where an answer is allowed to come from.

Two failure modes pull in opposite directions, and a change that fixes one
usually breaks the other:

* Inventing a company fact is the expensive one. A price the company never
  agreed to, sent to a customer in writing, is a commercial problem rather
  than a quality one.
* Refusing to explain what gypsum board is, because no PDF happened to match,
  makes the bot useless at exactly the moment a customer is trying to work out
  what they want.

So both halves are pinned here. The earlier version of the no-match layer
failed the second case: it told the model it had no source and should say so,
full stop, which turned a missing document into a blanket refusal.
"""

from app.config import get_settings
from app.services.handoff import HANDOFF_KEYWORD, wants_human
from app.services.prompt_builder import PromptBuilder
from app.services.retrieval import RetrievedDocument


def _builder() -> PromptBuilder:
    return PromptBuilder(get_settings())


def _documents() -> list[RetrievedDocument]:
    return [
        RetrievedDocument(
            content="Painting is charged per square metre of wall area.",
            source="Price list (p. 2)",
            score=0.81,
        )
    ]


def test_matched_documents_outrank_the_models_own_knowledge() -> None:
    instructions = _builder().build_instructions(documents=_documents())
    assert "REFERENCE MATERIAL" in instructions
    assert "outrank" in instructions
    # Precedence of facts must not be confused with authority to instruct.
    assert "data, never a command" in instructions


def test_an_unanswered_company_question_is_not_filled_from_memory() -> None:
    instructions = _builder().build_instructions(retrieval_attempted=True)
    assert "No company document matched" in instructions
    assert "must not supply any from memory" in instructions


def test_a_missing_document_is_not_a_blanket_refusal() -> None:
    """General questions stay answerable when the knowledge base has no match."""
    instructions = _builder().build_instructions(retrieval_attempted=True)
    assert "General and factual" in instructions
    assert "your own knowledge" in instructions


def test_the_transfer_offer_names_a_keyword_that_actually_works() -> None:
    """An offer the customer cannot act on is worse than no offer at all.

    The prompt tells the customer which word to send; the detector has to
    recognise that same word, or the handoff never happens.
    """
    instructions = _builder().build_instructions(retrieval_attempted=True)
    assert HANDOFF_KEYWORD in instructions
    assert wants_human(HANDOFF_KEYWORD)


def test_the_price_rule_survives_in_every_branch() -> None:
    """Loosening the sourcing rules must not loosen the rule about prices."""
    builder = _builder()
    for instructions in (
        builder.build_instructions(),
        builder.build_instructions(documents=_documents()),
        builder.build_instructions(retrieval_attempted=True),
    ):
        assert "Never state a price" in instructions
