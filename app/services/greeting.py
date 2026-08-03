"""Has this customer actually asked for anything yet?

A session's first message is one of two things, and they need opposite
treatment:

* ``mrhba``, ``good morning``, ``al-salam alaykum`` -- an opening. There is no
  question here, so there is nothing to answer. The welcome IS the reply.
* ``how much is finishing a flat`` -- a request. The welcome must not be sent
  on its own, because doing so leaves the customer's actual question hanging
  while a menu arrives instead.

Before this module the second case was handled and the first was not: anything
containing a letter went to the model, which was told the welcome had been
prepended and asked to continue from it. Given a bare greeting it dutifully
continued -- inventing a topic, or asking a question the welcome had just
asked. The customer got a greeting, a menu, and a redundant "how can I help?"
in one message.

Why a word set and not a phrase list
------------------------------------
The obvious implementation matches greeting phrases and checks whether the
message is one of them. It fails immediately on real traffic, because people
do not send bare greetings: they send ``ya bashmohandes izzayak``, ``salam
alaykum wa rahmat allah wa barakatuh``, ``hi :)``, ``sabah el kheir ya
fandem``. Each is an opening; none is in any reasonable phrase list.

So the test is inverted. Every word is checked against a set of greetings,
honorifics and pleasantries, and the message is an opening only when NOTHING
is left over. One unrecognised word -- ``price``, ``flat``, ``when``, a phone
number -- and it is a request. That makes the failure direction the safe one:
a greeting phrased unusually gets answered by the model, which is merely the
old behaviour, while a real question can never be swallowed by the greeting
path as long as it contains a single word that is not a pleasantry.

It also means the set can be extended without re-reasoning about precedence.
Adding a word can only ever move messages from "request" to "greeting", and
only for messages made up entirely of words already in the set.

Why it is checked only on the first message of a session
--------------------------------------------------------
Mid-conversation, ``tmam`` and ``shukran`` are turns in a dialogue the model
can see and should answer in context. This function is consulted by
``ChatService`` only when the session still owes its customer a welcome, so a
later ``thanks`` is never diverted.

Normalisation
-------------
Arabic is written several ways for the same word: ``ahlan`` appears with and
without the tanween, alef appears bare, with hamza above, with hamza below and
with madda. Matching raw text would need every spelling in the set, and would
still miss the next one. Instead the text is folded first -- diacritics
removed, alef forms unified, ta marbuta to ha -- and the set is written in the
folded form.

Punctuation and emoji are dropped before matching, so ``Hey!!`` and ``hi \U0001f44b``
are openings. Digits are NOT dropped: a message containing a phone number or
an area in square metres is a request, whatever else it contains.

Arabic is written as \\uXXXX escapes, matching ``intent.py`` and ``handoff.py``.
These are matching patterns rather than text a customer reads, so the tradeoff
that governs ``persona.py`` -- proofreadability of approved copy -- does not
apply, and immunity to tools that mangle bidirectional text does.
"""

import re

# Tashkeel and tatweel. Invisible decoration: "\u0623\u0647\u0644\u0627\u064b" and "\u0623\u0647\u0644\u0627" are one word.
_DIACRITICS = re.compile("[\u064b-\u0652\u0670\u0640]")

# Alef forms -> bare alef, alef maqsura -> ya, ta marbuta -> ha, and the two
# hamza carriers -> their base letters.
_FOLD = str.maketrans(
    {
        "\u0623": "\u0627",  # alef with hamza above
        "\u0625": "\u0627",  # alef with hamza below
        "\u0622": "\u0627",  # alef with madda
        "\u0671": "\u0627",  # alef wasla
        "\u0649": "\u064a",  # alef maqsura -> ya
        "\u0629": "\u0647",  # ta marbuta -> ha
        "\u0624": "\u0648",  # waw with hamza
        "\u0626": "\u064a",  # ya with hamza
    }
)

# Anything that is not a letter or a digit becomes a separator: punctuation,
# emoji, "!!!", "\u061f", ".". Digits survive deliberately -- see the module
# docstring.
_NOT_A_WORD = re.compile("[^0-9a-z\u0621-\u064a\u0660-\u0669]+")

# Greetings, honorifics and pleasantries. A message made ONLY of these has
# asked for nothing. Written in the folded form produced by _words().
_PLEASANTRIES = frozenset(
    {
        # --- Arabic greetings -------------------------------------------
        "\u0627\u0644\u0633\u0644\u0627\u0645",  # al-salam
        "\u0633\u0644\u0627\u0645",  # salam
        "\u0639\u0644\u064a\u0643\u0645",  # alaykum
        "\u0648\u0639\u0644\u064a\u0643\u0645",  # wa alaykum
        "\u0648\u0631\u062d\u0645\u0647",  # wa rahmat
        "\u0627\u0644\u0644\u0647",  # allah
        "\u0648\u0628\u0631\u0643\u0627\u062a\u0647",  # wa barakatuh
        "\u0633\u0644\u0627\u0645\u0627\u062a",  # salamat
        "\u0635\u0628\u0627\u062d",  # sabah - morning
        "\u0635\u0628\u0627\u062d\u0648",  # sabaho
        "\u0645\u0633\u0627\u0621",  # masaa - evening
        "\u0627\u0644\u062e\u064a\u0631",  # al-kheir
        "\u0627\u0644\u0646\u0648\u0631",  # al-noor
        "\u0627\u0644\u0641\u0644",  # al-full
        "\u0627\u0647\u0644\u0627",  # ahlan
        "\u0648\u0633\u0647\u0644\u0627",  # wa sahlan
        "\u0627\u0647\u0644\u064a\u0646",  # ahlein
        "\u0645\u0631\u062d\u0628\u0627",  # marhaba
        "\u0645\u0631\u062d\u0628\u062a\u064a\u0646",  # marhabtein
        "\u0647\u0627\u064a",  # hai
        "\u0647\u0644\u0627",  # hala
        "\u0647\u0627\u0644\u0648",  # halo
        "\u0627\u0644\u0648",  # alo
        "\u062a\u062d\u064a\u0627\u062a\u064a",  # tahiyati - regards
        # --- Arabic "how are you" ---------------------------------------
        "\u0627\u0632\u064a\u0643",  # izzayak
        "\u0627\u0632\u064a\u0643\u0645",  # izzayukum
        "\u0627\u0632\u0627\u064a\u0643",  # izzayak
        "\u0639\u0627\u0645\u0644",  # amel
        "\u0639\u0627\u0645\u0644\u0647",  # amela
        "\u0627\u064a\u0647",  # eih - what
        "\u0627\u0644\u0627\u062e\u0628\u0627\u0631",  # al-akhbar
        "\u0627\u062e\u0628\u0627\u0631\u0643",  # akhbarak
        "\u0643\u064a\u0641",  # keif
        "\u0643\u064a\u0641\u0643",  # keifak
        "\u062d\u0627\u0644\u0643",  # halak
        "\u0627\u0644\u062d\u0627\u0644",  # al-hal
        # --- Arabic politeness and honorifics ---------------------------
        "\u0634\u0643\u0631\u0627",  # shukran
        "\u0645\u062a\u0634\u0643\u0631",  # motashaker
        "\u062a\u0645\u0627\u0645",  # tamam
        "\u062d\u0636\u0631\u062a\u0643",  # hadretak
        "\u064a\u0627",  # ya
        "\u0648",  # wa
        "\u0644\u0648",  # law
        "\u0633\u0645\u062d\u062a",  # samaht - law samaht
        "\u0645\u0646",  # men
        "\u0641\u0636\u0644\u0643",  # fadlak - men fadlak
        "\u0628\u064a\u0643",  # beek - ahlan beek
        "\u0628\u0643",  # bek
        "\u0628\u0627\u0634\u0627",  # basha
        "\u0641\u0646\u062f\u0645",  # fandem
        "\u0627\u0633\u062a\u0627\u0630",  # ostaz
        "\u0645\u0647\u0646\u062f\u0633",  # mohandes
        "\u0628\u0634\u0645\u0647\u0646\u062f\u0633",  # bashmohandes
        "\u062f\u0643\u062a\u0648\u0631",  # doktor
        "\u0643\u0627\u0628\u062a\u0646",  # captain
        # --- English and transliterated ---------------------------------
        "hi",
        "hii",
        "hiii",
        "hiiii",
        "hey",
        "heyy",
        "heyyy",
        "hello",
        "helo",
        "hallo",
        "halo",
        "hullo",
        "yo",
        "sup",
        "greetings",
        "good",
        "morning",
        "afternoon",
        "evening",
        "night",
        "day",
        "today",
        "salam",
        "salaam",
        "salamu",
        "assalam",
        "assalamu",
        "assalamualaikum",
        "salamualaikum",
        "alaikum",
        "alaykum",
        "aleikum",
        "alaikom",
        "peace",
        "upon",
        "you",
        "how",
        "are",
        "is",
        "it",
        "going",
        "doing",
        "there",
        "thanks",
        "thank",
        "thx",
        "ty",
        "ok",
        "okay",
        "please",
        "pls",
        "sir",
        "madam",
        "maam",
        "mr",
        "mrs",
        "dear",
        "team",
        "ahlan",
        "marhaba",
        "marhaban",
        "hola",
        "bonjour",
        "ciao",
    }
)


def _words(text: str) -> list[str]:
    """Fold one message down to a list of comparable words."""
    folded = _DIACRITICS.sub("", text.casefold()).translate(_FOLD)
    return _NOT_A_WORD.sub(" ", folded).split()


def is_greeting_only(text: str | None) -> bool:
    """True when the message is an opening and carries no request.

    Returns False for a message that folds away to nothing -- a lone emoji, a
    full stop, whitespace. Those are not greetings, they are unintelligible,
    and ``persona.is_unintelligible`` already owns them and answers with the
    welcome plus an invitation to say what is needed. Returning True here
    would swallow that second half.
    """
    if not text:
        return False
    words = _words(text)
    if not words:
        return False
    return all(word in _PLEASANTRIES for word in words)
