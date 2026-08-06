"""The style guide: how the bot is allowed to sound, and how it is allowed to
look.

Style is usually not worth testing -- wording drift costs a little quality and
nothing else. These rules are the exceptions, because breaking them is either
invisible in review or actively misleading to a customer:

* The emoji rules are a property of the *sent message*, and the code prepends
  a welcome that already carries one. A rule stated in the persona alone would
  be violated by construction on every first reply.
* The formatting syntax rule is one careless edit from turning every reply
  into a screen of literal asterisks. Nothing else in the codebase would
  notice: the output is well-formed markdown, and no messaging app renders it.
* "Do not say you are an AI" is one edit away from "deny being an AI". The
  honesty half is pinned here so it cannot be dropped as redundant.
* The rules that must survive a SYSTEM_PROMPT override live in the response
  rules layer, and nothing else would notice if they quietly stopped being
  emitted.
* The response rules are emitted on every channel, so naming one of them
  there is a claim the layer cannot support. The word is easy to reintroduce
  and nothing but a test would catch it.
* Anger handling has to point at the handoff keyword that actually works,
  not at a second, invented escalation path -- and it has to suppress the
  decoration, which is a rule that only ever shows up in the conversations
  that have already gone wrong.
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


def _response_rules() -> str:
    """The response rules layer on its own.

    Built with a custom SYSTEM_PROMPT so the packaged Arabic persona is not in
    the way: the persona is allowed to name a platform, and asserting over the
    whole instruction block would only ever measure that.

    Sections are joined with a blank line and the rules themselves use single
    newlines between bullets, so splitting on the blank line isolates exactly
    one layer.
    """
    instructions = PromptBuilder(
        Settings(system_prompt="You are a terse robot.")
    ).build_instructions()
    return instructions[instructions.index("# Response rules") :].split("\n\n")[0]


def test_the_welcome_spends_exactly_one_emoji() -> None:
    """The approved copy has to obey the rule it is bundled with.

    The ceiling of five applies to a structured reply the model writes. The
    welcome is a fixed greeting, and a greeting carrying two would break the
    budget before the model has written a word.
    """
    assert len(_EMOJI.findall(persona.WELCOME)) == 1


def test_the_fixed_clarification_line_carries_no_emoji() -> None:
    assert _EMOJI.search(persona.NOT_UNDERSTOOD) is None


def test_the_first_reply_does_not_open_with_a_second_emoji() -> None:
    instructions = _builder().build_instructions(is_first_message=True)
    assert "already contains an emoji" in instructions
    assert "do not open this reply with another one" in instructions


def test_emoji_are_structural_in_both_layers() -> None:
    """Decoration is the failure mode: a row of emoji reads as a mass mailing."""
    for text in (persona.SYSTEM_PROMPT, _builder().build_instructions()):
        assert "Emoji are for structure" in text


def test_an_angry_customer_gets_no_emoji() -> None:
    """A tick beside a late handover reads as mockery."""
    assert "no emoji" in _builder().build_instructions()


def test_the_customer_is_addressed_formally() -> None:
    # hadretak - the respectful form of address.
    assert "\u062d\u0636\u0631\u062a\u0643" in persona.SYSTEM_PROMPT
    assert "respectful form of address" in _builder().build_instructions()


def test_slang_is_ruled_out_without_ruling_out_the_dialect() -> None:
    """Egyptian Arabic is required elsewhere; only slang is forbidden."""
    assert "slang is not" in persona.SYSTEM_PROMPT
    assert "Egyptian Arabic" in persona.SYSTEM_PROMPT


def test_replies_are_broken_into_short_paragraphs() -> None:
    """Length is now governed by paragraph shape rather than a line count.

    A structured list of services is legitimately longer than five lines and
    easier to read than the same content as prose. What is never allowed is
    the wall of text.
    """
    for text in (persona.SYSTEM_PROMPT, _builder().build_instructions()):
        assert "one to three lines" in text
        assert "Never a wall of text" in text


def test_bold_syntax_is_stated_in_both_layers() -> None:
    """Messaging apps render no markdown at all.

    ``**bold**`` and ``# Heading`` arrive as literal characters, so a model
    told to "use headings" without the syntax makes the reply worse than the
    plain text it replaced.

    The syntax is unchanged by the move to a channel-agnostic wording: a
    single asterisk each side is what WhatsApp renders, and it is still what
    both layers ask for.
    """
    for text in (persona.SYSTEM_PROMPT, _builder().build_instructions()):
        assert "single asterisk" in text


def test_the_response_rules_name_no_channel() -> None:
    """A layer sent on every channel may not claim to know which one it is on.

    The formatting bullet used to open with "WhatsApp does NOT render
    markdown". That is now false on four of the five channels the platform
    supports, and a rule the model can see is wrong about its own situation is
    a rule it is entitled to discount.

    The persona is deliberately not covered by this: it is copy for one
    WhatsApp-first business, and SYSTEM_PROMPT replaces it wholesale.
    """
    rules = _response_rules()
    # The rule survives; only its justification was generalised.
    assert "single asterisk" in rules
    assert "do NOT render markdown" in rules
    for channel_name in ("WhatsApp", "Messenger", "Instagram", "Facebook"):
        assert channel_name not in rules


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


def test_formatting_may_not_change_the_facts() -> None:
    """The tidier the list, the stronger the pull to even it up.

    Four uneven services look worse than five parallel ones, and inventing the
    fifth is the specific way a formatting instruction becomes a lie.
    """
    for text in (persona.SYSTEM_PROMPT, _builder().build_instructions()):
        assert "never content" in text


def test_the_style_rules_survive_a_custom_system_prompt() -> None:
    """Setting SYSTEM_PROMPT replaces the persona, not the response rules.

    A business reusing this codebase should not silently lose the formality,
    shape, formatting, emoji and AI-disclosure rules along with the Arabic.
    """
    builder = PromptBuilder(Settings(system_prompt="You are a terse robot."))
    instructions = builder.build_instructions()

    assert "You are a terse robot." in instructions
    assert persona.SYSTEM_PROMPT not in instructions
    for rule in (
        "respectful form of address",
        "one to three lines",
        "Emoji are for structure",
        "single asterisk",
        "never content",
        "never claim to be a human being",
        "apologise once",
        "Never state a price",
    ):
        assert rule in instructions
