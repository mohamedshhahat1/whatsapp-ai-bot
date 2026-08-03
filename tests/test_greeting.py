"""A greeting is not a question, and answering one anyway is how a bot sounds
like a bot.

The two tables below are not symmetric in importance.

``OPENINGS`` failing means a greeting reaches the model, which is exactly what
happened before this module existed. Mildly worse output, nothing lost.

``REQUESTS`` failing is the real failure: a customer who asked something is
answered with a welcome menu, their question is never sent to the model, and
they have to ask again. So the entries there are deliberately hostile --
questions wrapped in greetings, politeness and honorifics, which is how people
actually open a conversation with a business.
"""

from app.services.greeting import is_greeting_only

# Nothing has been asked. The welcome is the whole reply.
OPENINGS = (
    "Hi",
    "hello",
    "HEY",
    "Hey!!",
    "hiii",
    "Good morning",
    "good evening",
    "Good day, sir",
    "Salam",
    "salam alaikum",
    "assalamu alaikum",
    "how are you?",
    "Hello, how are you doing today?",
    "hi \U0001f44b",
    "   hello   ",
    "ok thanks",
    "السلام عليكم",
    "السلام عليكم ورحمة الله وبركاته",
    "وعليكم السلام",
    "صباح الخير",
    "مساء الخير",
    "صباح النور",
    "أهلاً",
    "اهلا وسهلا",
    "أهلاً بحضرتك",
    "ازيك",
    "إزيك؟",
    "مرحبا",
    "مرحبا،",
    "عامل ايه",
    "اخبارك ايه",
    "يا باشا ازيك",
    "أهلا يا بشمهندس",
    "لو سمحت",
    "تمام شكرا",
    "صباح الخير يا فندم",
)

# Something was asked. These must reach the model.
REQUESTS = (
    "عايز أعرف سعر تشطيب شقة",
    "السلام عليكم عايز عرض سعر",
    "مرحبا، بتعملوا تصميم 3D؟",
    "صباح الخير، ممكن معاينة؟",
    "أهلاً، ممكن أكلم المدير؟",
    "لو سمحت عايز أعرف خدماتكم",
    "محتاج تشطيب داخلي",
    "ابعتلي مشاريع سابقة",
    "Hi, do you provide 3D designs?",
    "Hello I want a quotation",
    "how much does apartment finishing cost?",
    "Good morning, I want a site visit",
    "Tell me about your services",
    "thanks, but when can you start?",
    "01000000000",
    "١٢٠",
)

# Handled by persona.is_unintelligible, which sends the welcome AND an
# invitation to say what is needed. Claiming these here would drop that half.
NO_WORDS = ("", "   ", ".", "...", "؟", "!!", "\U0001f44d", None)


def test_an_opening_with_no_request_is_a_greeting() -> None:
    for text in OPENINGS:
        assert is_greeting_only(text), text


def test_a_message_carrying_a_request_is_never_a_greeting() -> None:
    """One unrecognised word is enough. This is the half that must not break."""
    for text in REQUESTS:
        assert not is_greeting_only(text), text


def test_wordless_messages_are_left_to_is_unintelligible() -> None:
    for text in NO_WORDS:
        assert not is_greeting_only(text), text


def test_spelling_variants_of_the_same_greeting_all_match() -> None:
    """Folding, not a longer word list: alef and tanween vary by keyboard."""
    for text in ("أهلاً", "اهلا", "آهلا", "إهلا", "أهلا"):
        assert is_greeting_only(text), text


def test_digits_survive_folding() -> None:
    """A number is a request -- an area, a phone, an answer to a question."""
    assert not is_greeting_only("hi 120")
    assert not is_greeting_only("أهلاً ١٢٠ متر")
