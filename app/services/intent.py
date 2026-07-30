"""What is this message about, before spending a model call on it.

Three buckets:

* ``COMPANY``   -- about this business: services, branches, warranty,
  contracts, past projects, appointments. Retrieval runs and the documents
  answer it.
* ``DOMAIN``    -- about finishing and contracting in general: what gypsum
  board is, whether wiring comes before plaster, how long paint takes to dry.
  No company document is needed and none is searched.
* ``OUT``       -- the president of France. Answered from fixed copy, with no
  OpenAI call at all.

Why this is not a model call
----------------------------
Asking the model to classify before asking it to answer doubles the latency
and the bill to save one request, and it puts the scope guard behind the same
quota and outage as the thing it guards. A keyword pass costs microseconds and
is testable, which is the whole point: you can see in CI exactly which
messages get refused.

The bias, and why it goes this way
----------------------------------
This classifier is wrong sometimes. The question is which direction.

Refusing a real customer is the expensive error. Somebody asking a genuine
question in phrasing the keyword lists do not cover gets told to stay on
topic, and a customer told that by a bot does not rephrase -- they leave. A
few off-topic questions reaching the model costs a fraction of a cent each and
the persona already declines them politely.

So ``OUT`` requires all three of:

1. positive evidence of a foreign topic -- not merely the absence of company
   words,
2. no company or domain evidence anywhere in the message,
3. enough words to be judging something.

Anything unrecognised is ``COMPANY``, which is exactly what the bot does
today. This module can only ever narrow behaviour for messages it is confident
about.

The short-message rule matters more than it looks
-------------------------------------------------
Real conversations are full of turns that carry no topic at all: "\u0623\u064a\u0648\u0647",
"\u062a\u0645\u0627\u0645", "\u0648\u0627\u0644\u0641\u064a\u0644\u0627\u061f", "\u0643\u0627\u0645 \u064a\u0648\u0645\u061f", "120". Each one is meaningful only against the
previous turn, which this function cannot see. Classifying them on their own
words would refuse a customer in the middle of answering the bot's own
question, so anything under four words is never refused.

Arabic is written as \\uXXXX escapes, matching ``handoff.py``, so the file
stays pure ASCII and cannot be mangled by a tool that mishandles
bidirectional text. The transliteration is in the comment beside each group.
"""

import re

COMPANY = "company"
DOMAIN = "domain"
OUT = "out_of_scope"

# Below this many words, a message is treated as a follow-up to the previous
# turn and never refused. "who is the president of france" is seven words;
# "\u0648\u0627\u0644\u0641\u064a\u0644\u0627\u061f" is two.
_MIN_WORDS_TO_REFUSE = 4

# --- This company ------------------------------------------------------------
# Second-person plural is the strongest signal in Egyptian Arabic: "\u0639\u0646\u062f\u0643\u0645",
# "\u0628\u062a\u0639\u0645\u0644\u0648\u0627", "\u0634\u063a\u0644\u0643\u0645" all mean the customer is asking about *you*.
_COMPANY_TERMS = (
    "\u0634\u0631\u0643\u0629|\u0634\u0631\u0643\u062a\u0643\u0645"  # sharika / sharikatkom
    "|\u0641\u0631\u0639|\u0641\u0631\u0648\u0639"  # far' / furoo' - branch(es)
    "|\u0639\u0646\u062f\u0643\u0645|\u0639\u0646\u062f\u0643\u0648"  # 3andokom - do you have
    "|\u0628\u062a\u0639\u0645\u0644\u0648\u0627|\u0628\u062a\u0634\u062a\u063a\u0644\u0648\u0627"  # do you do / do you work
    "|\u062e\u062f\u0645\u0627\u062a\u0643\u0645|\u062e\u062f\u0645\u0627\u062a"  # khadamatkom - services
    "|\u0634\u063a\u0644\u0643\u0645|\u0634\u063a\u0644\u0643\u0648"  # shoghlokom - your work
    "|\u0636\u0645\u0627\u0646"  # damaan - warranty
    "|\u0639\u0642\u062f|\u0627\u0644\u0639\u0642\u062f"  # 3aqd - contract
    "|\u0645\u0639\u0627\u064a\u0646\u0629"  # mo3ayna - site inspection
    "|\u0645\u0634\u0631\u0648\u0639|\u0645\u0634\u0627\u0631\u064a\u0639"  # mashroo3 - project(s)
    "|\u062a\u0633\u0644\u064a\u0645"  # tasleem - handover
    "|\u0628\u0627\u0642\u0629|\u0628\u0627\u0642\u0627\u062a"  # baaqa - package(s)
    "|\u0645\u0648\u0639\u062f|\u0645\u0648\u0627\u0639\u064a\u062f"  # maw3ed - appointment(s)
    "|\u0635\u064a\u0627\u0646\u0629"  # siyana - maintenance
    "|\u0639\u0646\u0648\u0627\u0646"  # 3enwaan - address
    "|\u0639\u0631\u0636 \u0633\u0639\u0631"  # 3ard se3r - quotation
    "|\u0627\u0644\u0643\u064a\u0627\u0646"  # al-kayan
    r"|\bcompany\b|\bbranch\w*\b|\byour\s+servic\w*\b|\bwarrant\w*\b"
    r"|\bguarantee\b|\bcontract\b|\bquotation\b|\bportfolio\b"
    r"|\bdo\s+you\s+(do|offer|have|provide)\b|\bappointment\b"
    r"|\bsite\s+visit\b|\bhandover\b|\bmaintenance\b"
)

# --- Finishing and contracting in general ------------------------------------
_DOMAIN_TERMS = (
    "\u062a\u0634\u0637\u064a\u0628|\u062a\u0634\u0637\u064a\u0628\u0627\u062a"  # tashteeb - finishing
    "|\u0645\u0642\u0627\u0648\u0644\u0627\u062a"  # moqawalaat - contracting
    "|\u062c\u0628\u0633|\u062c\u0628\u0633\u0648\u0645"  # gebs - gypsum
    "|\u062f\u0647\u0627\u0646|\u062f\u0647\u0627\u0646\u0627\u062a|\u0628\u0648\u064a\u0647"  # dehaan / boya - paint
    "|\u0633\u064a\u0631\u0627\u0645\u064a\u0643"  # ceramic
    "|\u0628\u0648\u0631\u0633\u0644\u064a\u0646"  # porcelain
    "|\u0631\u062e\u0627\u0645"  # rokhaam - marble
    "|\u0623\u0631\u0636\u064a\u0627\u062a|\u0627\u0631\u0636\u064a\u0627\u062a"  # ardeyaat - flooring
    "|\u0628\u0627\u0631\u0643\u064a\u0647"  # parquet
    "|\u0633\u0628\u0627\u0643\u0629"  # sebaaka - plumbing
    "|\u0643\u0647\u0631\u0628\u0627\u0621|\u0643\u0647\u0631\u0628\u0627"  # kahrabaa - electrical
    "|\u0633\u0642\u0641|\u0623\u0633\u0642\u0641|\u0627\u0633\u0642\u0641"  # saqf - ceiling(s)
    "|\u0623\u0644\u0648\u0645\u064a\u062a\u0627\u0644|\u0627\u0644\u0648\u0645\u064a\u062a\u0627\u0644"  # aluminium
    "|\u062e\u0634\u0628"  # khashab - wood
    "|\u0639\u0632\u0644"  # 3azl - insulation
    "|\u0645\u062d\u0627\u0631\u0629"  # mahaara - plastering
    "|\u062f\u064a\u0643\u0648\u0631|\u062f\u064a\u0643\u0648\u0631\u0627\u062a"  # decor
    "|\u0625\u0636\u0627\u0621\u0629|\u0627\u0636\u0627\u0621\u0629"  # edaa'a - lighting
    "|\u062d\u0645\u0627\u0645|\u0645\u0637\u0628\u062e"  # hammaam / matbakh
    "|\u0634\u0642\u0629|\u0641\u064a\u0644\u0627|\u0645\u0643\u062a\u0628"  # apartment / villa / office
    "|\u0637\u0648\u0628|\u0623\u0633\u0645\u0646\u062a|\u0627\u0633\u0645\u0646\u062a"  # brick / cement
    "|\u0645\u0633\u0627\u062d\u0629|\u0645\u062a\u0631"  # area / metre
    "|\u062a\u0643\u064a\u064a\u0641"  # air conditioning
    "|\u0628\u0627\u0628|\u0623\u0628\u0648\u0627\u0628|\u0634\u0628\u0627\u0643"  # door(s) / window
    r"|\bfinish\w*\b|\bcontract\w*\b|\bgypsum\b|\bdrywall\b|\bplaster\w*\b"
    r"|\bpaint\w*\b|\bceramic\b|\bporcelain\b|\bmarble\b|\bflooring\b"
    r"|\bparquet\b|\bplumb\w*\b|\belectric\w*\b|\bceiling\b|\baluminium\b"
    r"|\baluminum\b|\binsulat\w*\b|\bdecor\w*\b|\blighting\b|\bbathroom\b"
    r"|\bkitchen\b|\bapartment\b|\bvilla\b|\bcement\b|\brenovat\w*\b"
    r"|\bsquare\s+met\w*\b|\bwiring\b|\btiles?\b"
)

# --- Clearly somebody else's business ----------------------------------------
# Only topics that cannot plausibly appear in a finishing enquiry. "\u0645\u062f\u064a\u0631"
# (manager) and "\u0645\u0628\u0627\u0631\u0627\u0629" are not interchangeable, but "\u0631\u0626\u064a\u0633" can appear in
# "\u0631\u0626\u064a\u0633 \u0627\u0644\u0639\u0645\u0627\u0644" (foreman) -- which is why company and domain terms are
# checked first and win.
_OUT_TERMS = (
    "\u0631\u0626\u064a\u0633"  # ra'ees - president
    "|\u0627\u0646\u062a\u062e\u0627\u0628\u0627\u062a|\u0633\u064a\u0627\u0633\u0629"  # elections / politics
    "|\u062d\u0631\u0628"  # harb - war
    "|\u0643\u0648\u0631\u0629|\u0645\u0628\u0627\u0631\u0627\u0629"  # kora / match
    "|\u0627\u0644\u0623\u0647\u0644\u064a|\u0627\u0644\u0632\u0645\u0627\u0644\u0643"  # Ahly / Zamalek
    "|\u0627\u0644\u0637\u0642\u0633"  # weather
    "|\u0641\u064a\u0644\u0645|\u0623\u063a\u0646\u064a\u0629|\u0645\u0633\u0644\u0633\u0644"  # film / song / series
    "|\u0648\u0635\u0641\u0629|\u0637\u0628\u062e"  # recipe / cooking
    "|\u062f\u0648\u0627\u0621|\u0637\u0628\u064a\u0628|\u0645\u0631\u0636"  # medicine / doctor / illness
    "|\u0639\u0627\u0635\u0645\u0629"  # capital city
    "|\u0642\u0635\u064a\u062f\u0629|\u0646\u0643\u062a\u0629"  # poem / joke
    "|\u0628\u0648\u0631\u0635\u0629|\u0639\u0645\u0644\u0629 \u0631\u0642\u0645\u064a\u0629"  # stock market / crypto
    "|\u062a\u0631\u062c\u0645\u0644\u064a"  # translate for me
    r"|\bpresident\b|\bprime\s+minister\b|\belection\w*\b|\bpolitic\w*\b"
    r"|\bwar\s+in\b|\bfootball\b|\bsoccer\b|\bworld\s+cup\b"
    r"|\bweather\b|\bforecast\b|\bmovie\b|\bfilm\b|\bsong\b|\bnetflix\b"
    r"|\brecipe\b|\bcook\w*\b|\bdoctor\b|\bmedicine\b|\bsymptom\w*\b"
    r"|\bcapital\s+of\b|\bpoem\b|\bjoke\b|\bbitcoin\b|\bcrypto\b"
    r"|\bstock\s+market\b|\btranslate\b|\bhomework\b|\bessay\b"
    r"|\bpython\b|\bjavascript\b|\bprogramming\b|\bwrite\s+me\s+code\b"
)

_COMPANY_RE = re.compile(_COMPANY_TERMS, re.IGNORECASE)
_DOMAIN_RE = re.compile(_DOMAIN_TERMS, re.IGNORECASE)
_OUT_RE = re.compile(_OUT_TERMS, re.IGNORECASE)

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def classify(text: str | None) -> str:
    """Bucket one customer message.

    Order is the design. Company evidence is checked before the out-of-scope
    list, so "\u0645\u064a\u0646 \u0631\u0626\u064a\u0633 \u0645\u062c\u0644\u0633 \u0625\u062f\u0627\u0631\u0629 \u0627\u0644\u0634\u0631\u0643\u0629\u061f" is a company question that
    happens to contain the word 'president', not a request for political
    trivia. Domain evidence wins for the same reason.

    Returns ``COMPANY`` for anything it cannot place, because that is the
    behaviour that existed before this module and the one that cannot lose a
    customer.
    """
    if not text or not text.strip():
        return COMPANY

    if _COMPANY_RE.search(text):
        return COMPANY

    if _DOMAIN_RE.search(text):
        return DOMAIN

    # A short message is a follow-up to a turn this function cannot see.
    # Refusing it would interrupt the bot's own qualifying questions.
    if len(_WORD.findall(text)) < _MIN_WORDS_TO_REFUSE:
        return COMPANY

    if _OUT_RE.search(text):
        return OUT

    return COMPANY


# --- The refusal -------------------------------------------------------------

_OUT_OF_SCOPE_AR = (
    "\u0623\u0646\u0627 \u0647\u0646\u0627 \u0644\u0645\u0633\u0627\u0639\u062f\u0629 \u062d\u0636\u0631\u062a\u0643 \u0641\u064a \u0623\u064a \u0627\u0633\u062a\u0641\u0633\u0627\u0631 \u0639\u0646 "
    "{scope}\u060c \u0623\u0648 \u0639\u0646 \u0623\u0639\u0645\u0627\u0644 \u0627\u0644\u062a\u0634\u0637\u064a\u0628\u0627\u062a \u0648\u0627\u0644\u0645\u0642\u0627\u0648\u0644\u0627\u062a \u0628\u0634\u0643\u0644 \u0639\u0627\u0645.\n"
    "\u0644\u0648 \u0639\u0646\u062f\u0643 \u0623\u064a \u0633\u0624\u0627\u0644 \u0641\u064a \u0627\u0644\u0645\u062c\u0627\u0644 \u062f\u0647\u060c \u0623\u0646\u0627 \u062c\u0627\u0647\u0632 \u0623\u0633\u0627\u0639\u062f\u0643."
)

# ...khadamaat sharikat X / khadamaatna
_SCOPE_WITH_NAME = "\u062e\u062f\u0645\u0627\u062a \u0634\u0631\u0643\u0629 {name}"
_SCOPE_WITHOUT_NAME = "\u062e\u062f\u0645\u0627\u062a\u0646\u0627"


def out_of_scope_reply(company_name: str = "") -> str:
    """Fixed copy for a question outside the business.

    Sent without calling OpenAI. Bilingual for the same reason as the handoff
    acknowledgement: no language detection has happened yet, and this may be
    the customer's first message.

    It does not apologise for being unable to help -- it says what it *can*
    help with. "Sorry, I can't answer that" invites a second attempt at the
    same question; naming the scope redirects instead.
    """
    name = (company_name or "").strip()
    scope = _SCOPE_WITH_NAME.format(name=name) if name else _SCOPE_WITHOUT_NAME
    english_scope = f"{name}'s services" if name else "our services"
    return (
        _OUT_OF_SCOPE_AR.format(scope=scope)
        + "\n\n"
        + f"I'd be glad to help with anything about {english_scope}, or about "
        "finishing and contracting work in general."
    )
