"""A session's first message is one of three things, and only one is an
opening.

The four tables below are not equally important.

``REQUESTS`` is the one that must never break. A false positive there means a
customer who asked something is answered with a welcome menu, their question
never reaches the model, and they have to ask again. So the entries are
deliberately hostile -- questions wrapped in greetings, honorifics and
politeness, which is how people actually open with a business.

``OPENINGS`` failing is mild: a greeting reaches the model, which is what
happened before this module existed.

``COURTESIES`` failing in the greeting direction is the specific bug this
category was added to fix -- "ok thanks" used to be in the greeting table and
returned the full welcome menu.

``NEITHER`` is the one a future edit is most likely to break. Those messages
are pure filler with no identifying word, and they must belong to no category
at all. Promoting an honorific like "fandem" from filler to greeting would
make "ya fandem" answer with a menu, and nothing else in this file would
notice.
"""

from app.services.greeting import is_courtesy_only, is_greeting_only

# A) Nothing has been asked. The full welcome is the whole reply.
OPENINGS = (
    "Hi",
    "hello",
    "HEY",
    "Hey!!",
    "hiii",
    "Good morning",
    "good evening",
    "Good day, sir",
    "Hello brother",
    "Salam",
    "salam alaikum",
    "assalamu alaikum",
    "peace be upon you",
    "how are you?",
    "Hello, how are you doing today?",
    "hi \U0001f44b",
    "   hello   ",
    "\u0627\u0644\u0633\u0644\u0627\u0645 \u0639\u0644\u064a\u0643\u0645",
    "\u0627\u0644\u0633\u0644\u0627\u0645 \u0639\u0644\u064a\u0643\u0645 \u0648\u0631\u062d\u0645\u0629 \u0627\u0644\u0644\u0647 \u0648\u0628\u0631\u0643\u0627\u062a\u0647",
    "\u0648\u0639\u0644\u064a\u0643\u0645 \u0627\u0644\u0633\u0644\u0627\u0645",
    "\u0635\u0628\u0627\u062d \u0627\u0644\u062e\u064a\u0631",
    "\u0645\u0633\u0627\u0621 \u0627\u0644\u062e\u064a\u0631",
    "\u0635\u0628\u0627\u062d \u0627\u0644\u0646\u0648\u0631",
    "\u0635\u0628\u0627\u062d \u0627\u0644\u062e\u064a\u0631 \u064a\u0627 \u0641\u0646\u062f\u0645",
    "\u0623\u0647\u0644\u0627\u064b",
    "\u0627\u0647\u0644\u0627 \u0648\u0633\u0647\u0644\u0627",
    "\u0623\u0647\u0644\u0627\u064b \u064a\u0627 \u0641\u0646\u062f\u0645",
    "\u0623\u0647\u0644\u0627 \u064a\u0627 \u0628\u0634\u0645\u0647\u0646\u062f\u0633",
    "\u0627\u0632\u064a\u0643",
    "\u0625\u0632\u064a\u0643\u061f",
    "\u064a\u0627 \u0628\u0627\u0634\u0627 \u0627\u0632\u064a\u0643",
    "\u0645\u0631\u062d\u0628\u0627",
    "\u0645\u0631\u062d\u0628\u0627\u060c",
    "\u0639\u0627\u0645\u0644 \u0627\u064a\u0647",
    "\u0627\u062e\u0628\u0627\u0631\u0643 \u0627\u064a\u0647",
)

# B) Something was asked. These must reach the model.
REQUESTS = (
    "\u0639\u0627\u064a\u0632 \u0623\u0639\u0631\u0641 \u0633\u0639\u0631 \u062a\u0634\u0637\u064a\u0628 \u0634\u0642\u0629",
    "\u0627\u0644\u0633\u0644\u0627\u0645 \u0639\u0644\u064a\u0643\u0645\u060c \u0639\u0627\u0648\u0632 \u0623\u0639\u0631\u0641 \u0627\u0644\u0633\u0639\u0631",
    "\u0635\u0628\u0627\u062d \u0627\u0644\u062e\u064a\u0631\u060c \u0645\u062d\u062a\u0627\u062c \u0645\u0639\u0627\u064a\u0646\u0629",
    "\u0623\u0647\u0644\u0627\u064b\u060c \u0647\u0644 \u0639\u0646\u062f\u0643\u0645 \u062a\u0635\u0645\u064a\u0645 3D\u061f",
    "\u0645\u0631\u062d\u0628\u0627\u060c \u0628\u062a\u0639\u0645\u0644\u0648\u0627 \u062a\u0635\u0645\u064a\u0645 3D\u061f",
    "\u0644\u0648 \u0633\u0645\u062d\u062a \u0639\u0627\u064a\u0632 \u0623\u0639\u0631\u0641 \u062e\u062f\u0645\u0627\u062a\u0643\u0645",
    "\u0645\u062d\u062a\u0627\u062c \u062a\u0634\u0637\u064a\u0628 \u062f\u0627\u062e\u0644\u064a",
    "\u0627\u0628\u0639\u062a\u0644\u064a \u0645\u0634\u0627\u0631\u064a\u0639 \u0633\u0627\u0628\u0642\u0629",
    "\u0645\u0645\u0643\u0646 \u0623\u0643\u0644\u0645 \u0627\u0644\u0645\u062f\u064a\u0631\u061f",
    "Hi, can I get a quotation?",
    "Hello I want a quotation",
    "how much does apartment finishing cost?",
    "I need a site visit",
    "Can I speak with the manager?",
    "Do you provide 3D designs?",
    "Send me your previous projects",
    "Tell me about your services",
    "thanks, but when can you start?",
    "01000000000",
    "\u0661\u0662\u0660",
    "hi 120",
)

# C) Courtesies and sign-offs. Not openings, whenever they arrive.
COURTESIES = (
    "\u0634\u0643\u0631\u0627\u064b",
    "\u0634\u0643\u0631\u0627",
    "\u062a\u0645\u0627\u0645",
    "\u062a\u0645\u0627\u0645 \u0634\u0643\u0631\u0627\u064b",
    "\u062a\u0645\u0627\u0645 \u0643\u062f\u0647",
    "\u0627\u0648\u0643\u064a",
    "\u0623\u0648\u0643",
    "\u062a\u0633\u0644\u0645",
    "\u0631\u0628\u0646\u0627 \u064a\u0628\u0627\u0631\u0643\u0644\u0643",
    "\u062c\u0632\u0627\u0643 \u0627\u0644\u0644\u0647 \u062e\u064a\u0631",
    "\u0634\u0643\u0631\u0627 \u064a\u0627 \u0628\u0634\u0645\u0647\u0646\u062f\u0633",
    "\u0645\u062a\u0634\u0643\u0631 \u062c\u062f\u0627",
    "\u0627\u0644\u0641 \u0634\u0643\u0631",
    "\u0645\u0627\u0634\u064a",
    "Thanks",
    "Thank you",
    "thanks a lot",
    "thanks for your help",
    "Appreciate it",
    "ok thanks",
    "Perfect, thank you!",
    "bye",
)

# Politeness with no identifying word in it. Neither category, by design.
NEITHER = (
    "\u064a\u0627 \u0641\u0646\u062f\u0645",
    "\u0644\u0648 \u0633\u0645\u062d\u062a",
    "\u0645\u0646 \u0641\u0636\u0644\u0643",
    "\u064a\u0627 \u0628\u0634\u0645\u0647\u0646\u062f\u0633",
    "sir",
    "please",
)

# Handled by persona.is_unintelligible, which sends the welcome AND an
# invitation to say what is needed. Claiming these here would drop that half.
NO_WORDS = ("", "   ", ".", "...", "\u061f", "!!", "\U0001f44d", None)


def test_an_opening_with_no_request_is_a_greeting() -> None:
    for text in OPENINGS:
        assert is_greeting_only(text), text
        assert not is_courtesy_only(text), text


def test_a_message_carrying_a_request_is_in_no_category() -> None:
    """One unrecognised word is enough. This is the half that must not break."""
    for text in REQUESTS:
        assert not is_greeting_only(text), text
        assert not is_courtesy_only(text), text


def test_a_courtesy_is_never_treated_as_an_opening() -> None:
    """Regression: "ok thanks" once returned the full welcome menu."""
    for text in COURTESIES:
        assert is_courtesy_only(text), text
        assert not is_greeting_only(text), text


def test_politeness_alone_identifies_nothing() -> None:
    """Honorifics are filler. Being addressed politely is not a greeting.

    This is what the three-set split buys, and what an edit that promoted an
    honorific to a greeting would quietly break.
    """
    for text in NEITHER:
        assert not is_greeting_only(text), text
        assert not is_courtesy_only(text), text


def test_wordless_messages_are_left_to_is_unintelligible() -> None:
    for text in NO_WORDS:
        assert not is_greeting_only(text), text
        assert not is_courtesy_only(text), text


def test_spelling_variants_of_the_same_greeting_all_match() -> None:
    """Folding, not a longer word list: alef and tanween vary by keyboard."""
    for text in (
        "\u0623\u0647\u0644\u0627\u064b",
        "\u0627\u0647\u0644\u0627",
        "\u0622\u0647\u0644\u0627",
        "\u0625\u0647\u0644\u0627",
    ):
        assert is_greeting_only(text), text


def test_the_two_kheirs_are_different_words() -> None:
    """\u0627\u0644\u062e\u064a\u0631 opens a conversation; \u062e\u064a\u0631 closes one. Same root, different
    tokens, different sets -- which is why the shared vocabulary between the
    two categories does not collide.
    """
    assert is_greeting_only("\u0635\u0628\u0627\u062d \u0627\u0644\u062e\u064a\u0631")
    assert is_courtesy_only("\u062c\u0632\u0627\u0643 \u0627\u0644\u0644\u0647 \u062e\u064a\u0631")


def test_digits_survive_folding() -> None:
    """A number is a request -- an area, a phone, an answer to a question."""
    assert not is_greeting_only("hi 120")
    assert not is_greeting_only("\u0623\u0647\u0644\u0627\u064b \u0661\u0662\u0660 \u0645\u062a\u0631")
