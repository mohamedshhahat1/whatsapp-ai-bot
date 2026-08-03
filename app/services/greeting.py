"""What kind of thing is this opening message?

A session's first message is one of three things, and they need three
different responses:

* ``mrhba``, ``good morning``, ``ya basha izzayak`` -- an OPENING. Nothing has
  been asked, so there is nothing to answer. The full welcome is the reply.
* ``how much is finishing a flat`` -- a REQUEST. The welcome must not be sent
  on its own, or the customer's actual question sits unanswered while a menu
  arrives instead. A short welcome is prepended to the answer, in one message.
* ``shukran``, ``tamam``, ``thank you`` -- a COURTESY. Not an opening at all,
  even when it happens to be the first message of a session. Welcoming
  somebody who just thanked you is the single most obviously robotic thing
  this bot could do, so no welcome is sent and the message takes the normal
  path.

Why a word set and not a phrase list
------------------------------------
The obvious implementation matches greeting phrases and checks whether the
message is one of them. It fails immediately on real traffic, because people
do not send bare greetings: they send ``ya bashmohandes izzayak``, ``salam
alaykum wa rahmat allah wa barakatuh``, ``Hello brother``, ``sabah el kheir ya
fandem``. Each is an opening; none is in any reasonable phrase list.

So the test is inverted. Every word is checked against a set, and the message
belongs to a category only when NOTHING is left over. One unrecognised word --
``price``, ``flat``, ``when``, a phone number -- and it is a request.

Why three sets and not two
--------------------------
The first version of this module simply put ``shukran`` and ``tamam`` in with
the greetings, which is how ``ok thanks`` came to be answered with a welcome
menu. Removing them is not sufficient, because the two categories genuinely
share vocabulary:

* ``allah`` is in ``wa rahmat allah wa barakatuh`` and in ``jazak allah khayr``
* ``you`` is in ``how are you`` and in ``thank you``
* ``ya`` and ``bashmohandes`` attach to either

With one flat set per category those shared words force a word into the wrong
category or out of both. So tokens are split three ways instead:

* ``_GREETINGS`` and ``_COURTESY`` hold the words that IDENTIFY a category
* ``_FILLER`` holds honorifics, politeness and connective words that identify
  nothing and are acceptable in either

A message is a greeting when every word is a greeting or filler AND at least
one is a greeting. Same rule for courtesy. Filler on its own -- ``ya fandem``,
``law samaht`` -- is neither, and goes to the model, which is right: being
addressed politely is not a request but it is not a greeting either.

This also resolves the shared words for free. ``allah``, ``you``, ``ya`` and
the honorifics are all filler, so ``jazak allah khayr`` is courtesy on the
strength of ``jazak`` and ``khayr``, and ``salam alaykum wa rahmat allah`` is a
greeting on the strength of ``salam`` and ``alaykum``. Note that Arabic
``al-khayr`` (in ``sabah al-khayr``) and ``khayr`` (in ``jazak allah khayr``)
are different tokens, so they can sit in different sets without conflict.

Why the order of the two checks matters
---------------------------------------
Courtesy is checked first, and is deliberately the more generous set. The two
categories have very different costs when wrong:

* A greeting false positive is expensive. The customer asked something, the
  question never reaches the model, and they get a menu. They have to ask
  again.
* A courtesy false positive is cheap. The message still goes to the model and
  is still answered; the only consequence is that no welcome is prepended.
* A courtesy false negative is also cheap -- a welcome prefix on a message
  that did not need one.

So courtesy can afford breadth and greeting cannot. Checking courtesy first
means a message that somehow satisfies both is treated as courtesy, which is
the cheaper mistake.

Why this is checked only on the first message of a session
----------------------------------------------------------
Mid-conversation these are ordinary turns the model can see and should answer
in context. ``ChatService`` consults this module only while the session still
owes its customer a welcome, so a later ``thanks`` is never diverted. The
courtesy category exists purely to stop a welcome being attached to it.

Normalisation
-------------
Arabic is written several ways for the same word: ``ahlan`` appears with and
without the tanween, alef appears bare, with hamza above, with hamza below and
with madda. Matching raw text would need every spelling in the sets, and would
still miss the next one. Instead the text is folded first -- diacritics
removed, alef forms unified, ta marbuta to ha -- and the sets are written in
the folded form.

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

# Words that IDENTIFY an opening greeting. A message needs at least one.
_GREETINGS = frozenset(
    {
        # --- Arabic ------------------------------------------------------
        "\u0627\u0644\u0633\u0644\u0627\u0645",  # al-salam
        "\u0633\u0644\u0627\u0645",  # salam
        "\u0639\u0644\u064a\u0643\u0645",  # alaykum
        "\u0648\u0639\u0644\u064a\u0643\u0645",  # wa alaykum
        "\u0633\u0644\u0627\u0645\u0627\u062a",  # salamat
        "\u0635\u0628\u0627\u062d",  # sabah - morning
        "\u0635\u0628\u0627\u062d\u0648",  # sabaho
        "\u0645\u0633\u0627\u0621",  # masaa - evening
        "\u0627\u0644\u062e\u064a\u0631",  # al-kheir (NOT khayr - see docstring)
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
        "\u0627\u0632\u064a\u0643",  # izzayak
        "\u0627\u0632\u064a\u0643\u0645",  # izzayukum
        "\u0627\u0632\u0627\u064a\u0643",  # izzayak
        "\u0639\u0627\u0645\u0644",  # amel - amel eih
        "\u0639\u0627\u0645\u0644\u0647",  # amela
        "\u0627\u062e\u0628\u0627\u0631\u0643",  # akhbarak
        "\u0627\u0644\u0627\u062e\u0628\u0627\u0631",  # al-akhbar
        "\u0643\u064a\u0641",  # keif
        "\u0643\u064a\u0641\u0643",  # keifak
        "\u062d\u0627\u0644\u0643",  # halak
        "\u0627\u0644\u062d\u0627\u0644",  # al-hal
        # --- English and transliterated -----------------------------------
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
        "morning",
        "afternoon",
        "evening",
        "night",
        "day",
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
        "how",
        "are",
        "doing",
        "going",
        "there",
        "ahlan",
        "marhaba",
        "marhaban",
        "hola",
        "bonjour",
        "ciao",
    }
)

# Words that IDENTIFY a courtesy or sign-off. A message needs at least one.
# Broader than the greeting set on purpose: see the docstring on cost.
_COURTESY = frozenset(
    {
        # --- Arabic ------------------------------------------------------
        "\u0634\u0643\u0631\u0627",  # shukran
        "\u0634\u0643\u0631",  # shukr - alf shukr
        "\u0645\u0634\u0643\u0648\u0631",  # mashkour
        "\u0645\u062a\u0634\u0643\u0631",  # motashaker
        "\u0645\u062a\u0634\u0643\u0631\u064a\u0646",  # motashakreen
        "\u062a\u0645\u0627\u0645",  # tamam
        "\u0627\u0648\u0643\u064a",  # okay
        "\u0627\u0648\u0643",  # ok
        "\u062a\u0633\u0644\u0645",  # teslam
        "\u062a\u0633\u0644\u0645\u064a",  # teslami
        "\u0633\u0644\u0645\u062a",  # salemt
        "\u064a\u0628\u0627\u0631\u0643\u0644\u0643",  # yebaraklak
        "\u064a\u0628\u0627\u0631\u0643",  # yebarek
        "\u064a\u062e\u0644\u064a\u0643",  # yekhalleek
        "\u062c\u0632\u0627\u0643",  # jazak
        "\u062c\u0632\u0627\u0643\u0645",  # jazakum
        "\u062e\u064a\u0631",  # khayr (NOT al-kheir)
        "\u062e\u064a\u0631\u0627",  # khayran
        "\u0627\u0644\u0639\u0641\u0648",  # al-afw
        "\u0645\u0627\u0634\u064a",  # mashi
        "\u062d\u0627\u0636\u0631",  # hader
        "\u0637\u064a\u0628",  # tayeb
        "\u0645\u0645\u062a\u0627\u0632",  # momtaz
        "\u0628\u0631\u0627\u0641\u0648",  # bravo
        "\u064a\u062f\u064a\u0643",  # yedeek - tesalem edeek
        "\u0648\u062f\u0627\u0639\u0627",  # wadaan - goodbye
        "\u0645\u0639\u0627\u0644\u0633\u0644\u0627\u0645\u0647",  # maa al-salama (joined)
        # --- English -------------------------------------------------------
        "thanks",
        "thank",
        "thx",
        "tnx",
        "ty",
        "ok",
        "okay",
        "okey",
        "kk",
        "appreciate",
        "appreciated",
        "great",
        "perfect",
        "excellent",
        "super",
        "cool",
        "awesome",
        "nice",
        "alright",
        "fine",
        "noted",
        "understood",
        "cheers",
        "bye",
        "goodbye",
        "welcome",
    }
)

# Honorifics, politeness and connective words. These identify NOTHING: they
# are acceptable in either category, and a message made only of these belongs
# to neither and goes to the model.
_FILLER = frozenset(
    {
        # --- Arabic honorifics and address ---------------------------------
        "\u064a\u0627",  # ya
        "\u0648",  # wa
        "\u062d\u0636\u0631\u062a\u0643",  # hadretak
        "\u0628\u0627\u0634\u0627",  # basha
        "\u0641\u0646\u062f\u0645",  # fandem
        "\u0627\u0633\u062a\u0627\u0630",  # ostaz
        "\u0645\u0647\u0646\u062f\u0633",  # mohandes
        "\u0628\u0634\u0645\u0647\u0646\u062f\u0633",  # bashmohandes
        "\u062f\u0643\u062a\u0648\u0631",  # doktor
        "\u0643\u0627\u0628\u062a\u0646",  # captain
        "\u0627\u062e\u064a",  # akhi - brother
        "\u0627\u062e\u062a\u064a",  # okhti - sister
        "\u062d\u0627\u062c",  # hag
        "\u062d\u0627\u062c\u0647",  # haga
        "\u0645\u062f\u0627\u0645",  # madam
        "\u0647\u0627\u0646\u0645",  # hanem
        "\u0628\u064a\u0647",  # beh
        "\u0628\u064a\u0643",  # beek - ahlan beek
        "\u0628\u0643",  # bek
        "\u0639\u0632\u064a\u0632\u064a",  # azizi - dear
        # --- Arabic politeness and connectives -----------------------------
        "\u0644\u0648",  # law
        "\u0633\u0645\u062d\u062a",  # samaht - law samaht
        "\u0645\u0646",  # men
        "\u0641\u0636\u0644\u0643",  # fadlak - men fadlak
        "\u0627\u064a\u0647",  # eih - amel eih
        "\u0627\u0644\u0644\u0647",  # allah - in both categories
        "\u0648\u0631\u062d\u0645\u0647",  # wa rahmat
        "\u0648\u0628\u0631\u0643\u0627\u062a\u0647",  # wa barakatuh
        "\u0631\u0628\u0646\u0627",  # rabbena - rabbena yebaraklak
        "\u0627\u0644\u0641",  # alf - alf shukr
        "\u062c\u062f\u0627",  # geddan - very
        "\u0644\u064a\u0643",  # leek
        "\u0644\u064a\u0643\u0645",  # leekum
        "\u0644\u0643",  # lak
        "\u0644\u0643\u0645",  # lakum
        "\u0639\u0644\u064a",  # ala (folded) - shukran ala
        "\u0643\u062f\u0647",  # keda - tamam keda
        # --- English -------------------------------------------------------
        "good",
        "be",
        "upon",
        "you",
        "u",
        "r",
        "it",
        "so",
        "very",
        "much",
        "a",
        "lot",
        "all",
        "today",
        "please",
        "pls",
        "sir",
        "madam",
        "maam",
        "mr",
        "mrs",
        "dear",
        "team",
        "brother",
        "bro",
        "sis",
        "sister",
        "boss",
        "buddy",
        "friend",
        "guys",
        "my",
        "your",
        "and",
        "for",
        "help",
        "everything",
    }
)


def _words(text: str) -> list[str]:
    """Fold one message down to a list of comparable words."""
    folded = _DIACRITICS.sub("", text.casefold()).translate(_FOLD)
    return _NOT_A_WORD.sub(" ", folded).split()


def _is_only(text: str | None, identifying: frozenset[str]) -> bool:
    """True when every word is identifying or filler, and one is identifying.

    Returns False for a message that folds away to nothing -- a lone emoji, a
    full stop, whitespace. Those are not greetings, they are unintelligible,
    and ``persona.is_unintelligible`` already owns them and answers with the
    welcome plus an invitation to say what is needed. Claiming them here would
    swallow that second half.
    """
    if not text:
        return False
    words = _words(text)
    if not words:
        return False
    if not any(word in identifying for word in words):
        return False
    return all(word in identifying or word in _FILLER for word in words)


def is_courtesy_only(text: str | None) -> bool:
    """True for a thank-you, an acknowledgement or a sign-off.

    Check this BEFORE ``is_greeting_only``. A message that satisfies both is
    courtesy, which is the cheaper of the two mistakes to make.
    """
    return _is_only(text, _COURTESY)


def is_greeting_only(text: str | None) -> bool:
    """True when the message opens a conversation and carries no request."""
    if is_courtesy_only(text):
        return False
    return _is_only(text, _GREETINGS)
