"""Detecting that a customer wants a human being.

Deterministic regex, not a model call. Three reasons:

* A customer who writes "stop talking to the bot" must not depend on the bot
  being available, in budget and under its rate limit for that request to be
  honoured. Asking OpenAI to classify it puts the escalation path behind the
  thing being escalated away from.
* It is free and adds no latency to a message that is already frustrating.
* It is unit-testable, so a regression is caught in CI rather than by a
  customer.

The cost is coverage: unusual phrasings will be missed and the operator has to
notice them in the dashboard. That is the right failure direction -- a missed
phrase means the bot keeps helping, while a false positive means the bot goes
silent and a customer waits for a human who was never needed.

Patterns therefore require intent, not just a keyword. Bare "manager" does not
match, because "do you have a project manager for site visits?" is an ordinary
question for a finishing business.
"""

import re

# --- Arabic vocabulary -------------------------------------------------------
# Written as \u escapes so the source file stays pure ASCII and cannot be
# mangled by an editor, terminal or diff tool that mishandles bidirectional
# text. The transliteration in each comment is the readable form.

# aayez / aawez / ureed / ureed(hamza) / mehtaag / momken
_ASK = (
    "\u0639\u0627\u064a\u0632"
    "|\u0639\u0627\u0648\u0632"
    "|\u0627\u0631\u064a\u062f"
    "|\u0623\u0631\u064a\u062f"
    "|\u0645\u062d\u062a\u0627\u062c"
    "|\u0645\u0645\u0643\u0646"
)

# modeer (manager) / mowazzaf (employee) / mas'ool (person in charge) /
# bashar (human) / insaan (human)
_PERSON = (
    "\u0645\u062f\u064a\u0631"
    "|\u0645\u0648\u0638\u0641"
    "|\u0645\u0633\u0624\u0648\u0644"
    "|\u0628\u0634\u0631"
    "|\u0627\u0646\u0633\u0627\u0646"
)

# mesh / laa / maa / balaash
_NOT = (
    "\u0645\u0634"
    "|\u0644\u0627"
    "|\u0645\u0627"
    "|\u0628\u0644\u0627\u0634"
)

# bot (also matches robot, which contains it)
_BOT = "\u0628\u0648\u062a"

# khedmat al-omalaa - customer service
_CUSTOMER_SERVICE = (
    "\u062e\u062f\u0645\u0629 \u0627\u0644\u0639\u0645\u0644\u0627\u0621"
)

# kallemni - call me
_CALL_ME = "\u0643\u0644\u0645\u0646\u064a"

_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # "speak to a human", "talk with someone", "connect me to an agent"
        r"\b(speak|talk|chat|connect)\w*\s+(to|with)\s+(a|an|the)?\s*"
        r"(human|person|someone|somebody|agent|representative|rep|operator"
        r"|manager|supervisor|owner)\b",
        r"\breal\s+(person|human|agent)\b",
        r"\bhuman\s+(agent|being|operator|support)\b",
        r"\blive\s+(agent|person|chat|support)\b",
        r"\bcustomer\s+(service|support|care)\b",
        r"\b(call|phone|ring)\s+me\b",
        r"\bsomeone\s+(call|contact|phone|reach)\b",
        # "I want the manager", "I need an agent". A verb is required, so
        # "a project manager" and "the sales rep visited" do not match.
        r"\b(want|need|get|give)\s+(me\s+)?(a|an|the)?\s*"
        r"(manager|supervisor|representative|agent|operator)\b",
        # "I don't want to interact with the bot"
        r"\b(don'?t|do\s+not|dont)\s+want\s+(to\s+\w+\s+)?(with\s+)?"
        r"(a|an|the)?\s*(bot|robot|ai|machine)\b",
        r"\b(stop|no\s+more)\s+(the\s+)?(bot|robot|ai)\b",
        r"\btransfer\s+me\b",
        r"\bescalate\b",
        # Arabic: a request word within a short distance of a person word,
        # rather than the person word alone, for the same reason as above.
        f"({_ASK}).{{0,25}}({_PERSON})",
        f"({_NOT}).{{0,15}}({_BOT})",
        _CUSTOMER_SERVICE,
        _CALL_ME,
    )
)

# Sent once, when the conversation switches to a human. Silence would be worse:
# the customer asked for a person and would otherwise see nothing happen.
# Bilingual because the bot serves customers who write in either language and
# no language detection has happened at this point.
HANDOFF_ACK = (
    "\u0633\u064a\u062a\u0645 \u0627\u0644\u0631\u062f "
    "\u0639\u0644\u064a\u0643 \u0645\u0646 \u0627\u062d\u062f "
    "\u0645\u0648\u0638\u0641\u064a\u0646\u0627 "
    "\u0642\u0631\u064a\u0628\u0627.\n"
    "Thanks - I am passing you to a colleague. "
    "Someone will reply here shortly."
)


def wants_human(text: str | None) -> bool:
    """True when the message is a request to stop dealing with the bot.

    Media captions are passed here too, so ``None`` is a normal input and is
    not a request.
    """
    if not text:
        return False
    return any(pattern.search(text) for pattern in _PATTERNS)
