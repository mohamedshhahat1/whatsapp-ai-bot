"""The style guide: how the bot is allowed to sound.

Style is usually not worth testing -- wording drift costs a little quality and
nothing else. Four of these rules are exceptions, because breaking them is
either invisible in review or actively misleading to a customer:

* The emoji budget is a property of the *sent message*, and the code prepends
  a welcome that already spends it. A rule saying "one emoji" in the persona
  alone would be violated by construction on every first reply.
* "Do not say you are an AI" is one edit away from "deny being an AI". The
  honesty half is pinned here so it cannot be dropped as redundant.
* The rules that must survive a SYSTEM_PROMPT override live in the response
  rules layer, and nothing else would notice if they quietly stopped being
  emitted.
* Anger handling has to point at the handoff keyword that actually works,
  not at a second, invented escalation path.
"""

import re

from app.config import Settings, get_settings
from app.services import persona
from app.services.handoff import HANDOFF_KEYWORD
from app.services.prompt_builder import PromptBuilder

# Pictographs, dingbats and the variation selector. Deliberately excludes the
# bullet U+2022 used in WELCOME, which is punctuation, not decoration.
_EMOJI = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff\ufe0f]")


def _builder() -> PromptBuilder:
    return PromptBuilder(get_settings())


def test_the_welcome_spends_exactly_one_emoji() -> None:
    """The approved copy has to obey the rule it is bundled with.

    If the welcome ever carries two, the first reply breaks the budget before
    the model has written a word.
    """
    assert len(_EMOJI.findall(persona.WELCOME)) == 1


def test_the_fixed_clarification_line_carries_no_emoji() -> None:
    assert _EMOJI.search(persona.NOT_UNDERSTOOD) is None


def test_the_first_reply_is_told_not_to_add_a_second_emoji() -> None:
    instructions = _builder().build_instructions(is_first_message=True)
    assert "already contains an emoji" in instructions
    assert "add none of your own" in instructions


def test_the_emoji_budget_is_stated_in_every_reply() -> None:
    assert "At most one emoji" in _builder().build_instructions()


def test_the_customer_is_addressed_formally() -> None:
    # hadretak - the respectful form of address.
    assert "\u062d\u0636\u0631\u062a\u0643" in persona.SYSTEM_PROMPT
    assert "respectful form of address" in _builder().build_instructions()


def test_slang_is_ruled_out_without_ruling_out_the_dialect() -> None:
    """Egyptian Arabic is required elsewhere; only slang is forbidden."""
    assert "slang is not" in persona.SYSTEM_PROMPT
    assert "Egyptian Arabic" in persona.SYSTEM_PROMPT


def test_replies_are_kept_short() -> None:
    assert "five short lines" in persona.SYSTEM_PROMPT
    assert "five short lines" in _builder().build_instructions()


def test_being_an_ai_is_not_announced_but_never_denied() -> None:
    """The discretion half must not be allowed to become a licence to lie."""
    for text in (persona.SYSTEM_PROMPT, _builder().build_instructions()):
        assert "Do not announce that you are an AI" in text
        assert "never claim to be a human being" in text


def test_an_angry_customer_gets_one_apology_and_a_way_out() -> None:
    instructions = _builder().build_instructions()
    assert "apologise once" in instructions
    assert "Never promise compensation" in instructions
    # The offer has to name the word the detector recognises.
    assert HANDOFF_KEYWORD in instructions


def test_no_company_facts_are_invented() -> None:
    assert "never invent a company detail" in persona.SYSTEM_PROMPT


def test_the_style_rules_survive_a_custom_system_prompt() -> None:
    """Setting SYSTEM_PROMPT replaces the persona, not the response rules.

    A business reusing this codebase should not silently lose the formality,
    length, emoji and AI-disclosure rules along with the Arabic.
    """
    builder = PromptBuilder(Settings(system_prompt="You are a terse robot."))
    instructions = builder.build_instructions()

    assert "You are a terse robot." in instructions
    assert persona.SYSTEM_PROMPT not in instructions
    for rule in (
        "respectful form of address",
        "five short lines",
        "At most one emoji",
        "never claim to be a human being",
        "apologise once",
        "Never state a price",
    ):
        assert rule in instructions
