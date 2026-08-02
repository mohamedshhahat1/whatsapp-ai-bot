"""The pricing rule: no financial figure ever reaches a customer.

The company's rule is absolute -- no price, range, estimate, per-metre rate,
discount, deposit or total, under any circumstances, *including* figures that
exist in the knowledge base. That last clause is what makes this a code module
rather than a paragraph in the persona.

Why not just instruct the model
-------------------------------
An instruction is a strong bias, not a guarantee. Three things defeat one:

* A retrieved document that contains a number. The model has been told for its
  whole context that documents outrank its own knowledge, and now it is being
  told to ignore the one thing it was told to trust.
* Persistence. A customer who names a figure and asks you to meet it is
  running the pressure that works on people.
* ``SYSTEM_PROMPT``. It replaces the packaged persona wholesale, so a persona
  paragraph can be configured away by accident.

So the rule is enforced at four points, of which only one is a prompt:

1. ``redact`` strips amounts from retrieved chunks before the prompt is built.
2. ``instruction_layer`` is appended last and outranks the documents.
3. ``mentions_amount`` scans the generated reply; a reply containing an amount
   is thrown away and replaced with ``deflection``.
4. ``is_negotiating`` sends the conversation to a human immediately.

Two customer intents, two different responses
---------------------------------------------
These are not the same event and must not share a code path:

* **Asking** -- "how much is gypsum board?", "what do you charge per metre?"
  This is an ordinary opening question from someone who has not decided
  anything yet. The bot deflects, explains what a price depends on, and points
  at the Sales Manager. Handing every such customer to a person on their first
  message would put a human on the other end of every conversation that starts
  with the most common question in the business.

* **Negotiating** -- "ok do it for 1500", "that's expensive", "give me a
  discount", "Facebook says cheaper", "what's your final price?". A number is
  now on the table, or a position is. There is nothing here a bot can safely
  do: agreeing is a commitment it cannot make, refusing is a negotiation it
  cannot conduct, and deflecting a second time reads as stonewalling. It goes
  to a person on the first message.

The previous version counted price questions and escalated on the third. That
was wrong in both directions: it made a customer who was already haggling wait
through two more deflections, and it escalated a curious customer who had
simply asked three separate reasonable questions.

Why redaction is narrow
-----------------------
Redaction used to remove any digit near a thousands word or a percent sign.
Applied to a real knowledge base that turns "ضمان 10 سنوات" and "التنفيذ 45
يوم" and "تأسست عام 2018" into placeholders -- the bot then cannot answer a
warranty question, a timeline question or a question about the company, and
the documents that were supposed to make it useful have been shredded on the
way in.

A number is now only money when something says it is: a currency token, a
price word, a per-metre unit, or a thousands word with no unit after it. And a
number followed by سنة, يوم, متر, غرفة or similar is never money, whatever
else is near it.

This file is written in real Arabic rather than \\uXXXX escapes because it
contains copy that customers read.
"""

import re

# Reason recorded on the conversation when a negotiation forces a handoff.
SALES_HANDOFF_REASON = "customer_started_negotiating"

# What a redacted figure becomes inside a retrieved document. Visible rather
# than silent: the model should understand that a value was withheld, so it
# says "a colleague will quote that" instead of "the document does not say".
REDACTED = "[\u0633\u0639\u0631 \u0645\u062d\u062c\u0648\u0628]"

# A bare number at or above this is treated as money when nothing else
# explains it. Below it, the likeliest reading in this business is an area, a
# room count or a number of days -- and the bot asks for exactly those while
# qualifying a lead, so a lower bound here would hand "120" to sales in the
# middle of the question that was supposed to produce a quotation.
NEGOTIATION_MIN_AMOUNT = 1000

# --- Vocabulary --------------------------------------------------------------

_DIGIT = r"[0-9\u0660-\u0669]"
_NUMBER = rf"{_DIGIT}[0-9\u0660-\u0669,\u066c\u066b\.]{0,12}"

_CURRENCY = (
    r"\u062c\u0646\u064a\u0647\u0627\u062a"  # gunayhaat
    r"|\u062c\u0646\u064a\u0647"  # gunayh
    r"|\u062c\u0646\u064a\u0629"  # common misspelling
    r"|\u062c\.?\s?\u0645(?![\u0621-\u064a])"  # g.m
    r"|\u0631\u064a\u0627\u0644"  # riyal
    r"|\u062f\u0648\u0644\u0627\u0631"  # dollar
    r"|\u064a\u0648\u0631\u0648"  # euro
    r"|\u062f\u0631\u0647\u0645"  # dirham
    r"|EGP|SAR|AED|KWD|USD|EUR|GBP"
    r"|L\.?E\.?(?![A-Za-z])|\bLE\b"
    r"|pounds?|dollars?|riyals?|euros?"
)

# Units that make a number a measurement. A number followed by one of these is
# never money, whatever else is in the sentence.
_UNIT = (
    r"\u0645\u062a\u0631"  # metre
    r"|\u0623\u0645\u062a\u0627\u0631|\u0627\u0645\u062a\u0627\u0631"  # metres
    r"|\u0645\u00b2|\u0645 ?2"  # m2
    r"|\u062a\u0648\u0645|\u0623\u062a\u0627\u0645|\u0627\u062a\u0627\u0645"  # day(s)
    r"|\u0623\u0633\u0628\u0648\u0639|\u0627\u0633\u0627\u0628\u062a\u0639"  # week(s)
    r"|\u0634\u0647\u0631|\u0634\u0647\u0648\u0631|\u0623\u0634\u0647\u0631"  # month(s)
    r"|\u0633\u0646\u0629|\u0633\u0646\u0648\u0627\u062a|\u0633\u0646\u062a\u0646"  # year(s)
    r"|\u0633\u0627\u0639\u0629|\u0633\u0627\u0639\u0627\u062a"  # hour(s)
    r"|\u063a\u0631\u0641\u0629|\u063a\u0631\u0641"  # room(s)
    r"|\u062d\u0645\u0627\u0645|\u062d\u0645\u0627\u0645\u0627\u062a"  # bathroom(s)
    r"|\u062f\u0648\u0631|\u0623\u062f\u0648\u0627\u0631"  # floor(s)
    r"|\u0637\u0628\u0642\u0629|\u0637\u0628\u0642\u0627\u062a"  # coat(s)
    r"|\u0642\u0637\u0639\u0629|\u0642\u0637\u0639"  # piece(s)
    r"|\u0633\u0645|\u0645\u0645"  # cm / mm
    r"|met(?:er|re)s?|days?|weeks?|months?|years?|hours?|rooms?|cm|mm"
)

# Words that mark the number beside them as a price.
_PRICE_WORD = (
    r"\u0633\u0639\u0631|\u0627\u0644\u0633\u0639\u0631|\u0628\u0633\u0639\u0631"  # se'r
    r"|\u0623\u0633\u0639\u0627\u0631|\u0627\u0633\u0639\u0627\u0631"  # as'aar
    r"|\u062a\u0643\u0644\u0641\u0629|\u0627\u0644\u062a\u0643\u0644\u0641\u0629"  # taklifa
    r"|\u062a\u0643\u0644\u0641|\u064a\u0643\u0644\u0641"  # costs
    r"|\u062e\u0635\u0645|\u0645\u0642\u062f\u0645|\u062f\u0641\u0639\u0629|\u0642\u0633\u0637"
    r"|\u0645\u0628\u0644\u063a"  # amount
    r"|prices?|pricing|costs?|costing|discount|deposit|quote|quotation"
    r"|instal?ment|fee|budget"
)

# Per-square-metre notation, which makes a number a rate even with no currency.
_PER_METRE = (
    r"/\s?\u0645\u00b2|/\s?m\u00b2|/\s?m2"
    r"|per\s+(?:square\s+)?met(?:er|re)"
    r"|\u0644\u0644\u0645\u062a\u0631"  # lil-metr - per metre
    r"|\u0627\u0644\u0645\u062a\u0631 \u0627\u0644\u0645\u0631\u0628\u0639"
)

_THOUSANDS = (
    r"\u0623\u0644\u0641|\u0627\u0644\u0641"  # alf
    r"|\u0622\u0644\u0627\u0641|\u0627\u0644\u0627\u0641"  # alaaf
    r"|\u0645\u0644\u062a\u064a\u0648\u0646|\u0645\u0644\u0627\u064a\u062a\u0646"  # million(s)
    r"|k\b"
)

# Nothing that looks like a measurement may follow the number.
_NOT_A_UNIT = rf"(?!\s*(?:{_UNIT})\b)"

# --- Money patterns ----------------------------------------------------------
# Every pattern captures the part to hide in a group named ``amt``. Only that
# group is replaced, so "سعر المتر 2500" becomes "سعر المتر [سعر محجوب]"
# and the sentence still reads.

_MONEY = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # 2500 EGP / ٢٥٠٠ جنيه / 1,500.00 pounds
        rf"(?P<amt>{_NUMBER}\s*(?:{_CURRENCY}))",
        # EGP 2500 / جنيه ٢٥٠٠
        rf"(?P<amt>(?:{_CURRENCY})\s*{_NUMBER})",
        # $2500 / €2000 -- symbols have no word boundary
        rf"(?P<amt>[$\u20ac\u00a3]\s*{_NUMBER})",
        rf"(?P<amt>{_NUMBER}\s*[$\u20ac\u00a3])",
        # سعر المتر 2500 / the price is 2500 / خصم 500
        rf"(?:{_PRICE_WORD})[^0-9\u0660-\u0669\n]{0,25}(?P<amt>{_NUMBER}){_NOT_A_UNIT}",
        # 2500 للمتر / 2500 per square metre / 2500/m²
        rf"(?P<amt>{_NUMBER})\s*(?:{_PER_METRE})",
        # 50 ألف / 2 مليون / 50k -- but not "50 ألف متر"
        rf"(?P<amt>{_NUMBER}\s*(?:{_THOUSANDS})){_NOT_A_UNIT}",
        # خصم 10% / 10% discount. A bare percentage is NOT money: "رطوبة 60%"
        # and "زيادة 5% في المساحة" are ordinary facts.
        rf"(?:\u062e\u0635\u0645|discount)[^0-9\u0660-\u0669\n]{0,15}"
        rf"(?P<amt>{_NUMBER}\s*%)",
        rf"(?P<amt>{_NUMBER}\s*)[^0-9\u0660-\u0669\n]{0,15}"
        rf"(?:\u062e\u0635\u0645|discount)",
    )
)


def _strip_phone(text: str, sales_phone: str = "") -> str:
    """Remove the configured sales number before scanning for amounts.

    The deflection message contains a phone number, which is a long run of
    digits. Without this, the approved reply would trip the detector that the
    approved reply exists to satisfy.
    """
    cleaned = text
    phone = (sales_phone or "").strip()
    if phone:
        cleaned = cleaned.replace(phone, " ")
        bare = re.sub(r"\D", "", phone)
        if len(bare) >= 7:
            cleaned = cleaned.replace(bare, " ")
    return cleaned


def mentions_amount(text: str | None, sales_phone: str = "") -> bool:
    """True when the text contains something a customer would read as a price.

    Used on the model's *output*, as the last gate before a reply is sent.
    """
    if not text:
        return False
    return any(p.search(_strip_phone(text, sales_phone)) for p in _MONEY)


def _hide(match: re.Match[str]) -> str:
    """Replace only the captured amount, leaving the surrounding words."""
    whole = match.group(0)
    amount = match.group("amt")
    return whole.replace(amount, REDACTED, 1)


def redact(content: str) -> str:
    """Replace every monetary amount in a retrieved document.

    Narrow on purpose. Durations, areas, room counts, warranty periods and
    years survive untouched -- they are most of what makes the knowledge base
    worth retrieving, and an over-eager pass here silently degrades every
    answer the bot gives, not just the pricing ones.
    """
    cleaned = content
    for pattern in _MONEY:
        cleaned = pattern.sub(_hide, cleaned)
    return cleaned


# --- Asking about price (deflect) --------------------------------------------

_ASK_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\u0633\u0639\u0631|\u0627\u0633\u0639\u0627\u0631|\u0623\u0633\u0639\u0627\u0631",
        r"\u0628\u0643\u0627\u0645|\u0628\u0643\u0645",  # bekaam
        r"\u062a\u0643\u0644\u0641|\u062a\u0643\u0627\u0644\u064a\u0641",
        r"\u0645\u062a\u0632\u0627\u0646\u062a\u0629",  # budget
        r"\u062a\u0642\u0633\u062a\u0637|\u0623\u0642\u0633\u0627\u0637|\u0627\u0642\u0633\u0627\u0637",
        r"\u062f\u0641\u0639\u0629|\u0645\u0642\u062f\u0645",
        r"\bhow\s+much\b",
        r"\bpric\w*\b",
        r"\bcosts?\b|\bcosting\b",
        r"\bquot\w*\b",
        r"\bbudget\b",
        r"\binstal?lment\w*\b",
        r"\bdeposit\b",
        r"\bafford\w*\b",
        r"\bper\s+(?:square\s+)?met(?:er|re)\b",
    )
)


def asks_about_price(text: str | None) -> bool:
    """True when a customer message raises money as a question.

    Informational only. This does NOT escalate -- the bot answers with the
    deflection and keeps the conversation. See ``is_negotiating`` for the
    intent that does escalate.
    """
    if not text:
        return False
    return any(p.search(text) for p in _ASK_PATTERNS)


# --- Negotiating (immediate handoff) -----------------------------------------

_NEGOTIATION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # "expensive" / "too much" / "cheaper"
        r"\u063a\u0627\u0644\u064a|\u063a\u0627\u0644\u064a\u0629|\u0645\u0643\u0644\u0641",
        r"\u0631\u062e\u062a\u0635|\u0627\u0631\u062e\u0635|\u0623\u0631\u062e\u0635",
        r"\bexpensive\b|\btoo\s+much\b|\bcheaper\b|\bpricey\b",
        # "final price" / "last word" / "best price"
        r"\u0622\u062e\u0631 \u0633\u0639\u0631|\u0627\u062e\u0631 \u0633\u0639\u0631",
        r"\u0622\u062e\u0631 \u0643\u0644\u0627\u0645|\u0627\u062e\u0631 \u0643\u0644\u0627\u0645",
        r"\u0627\u0644\u0633\u0639\u0631 \u0627\u0644\u0646\u0647\u0627\u0626\u064a",
        r"\u0623\u062d\u0633\u0646 \u0633\u0639\u0631|\u0627\u062d\u0633\u0646 \u0633\u0639\u0631",
        r"\bfinal\s+(?:price|offer)\b|\bbest\s+(?:price|offer)\b",
        r"\blast\s+price\b",
        # asking for a discount
        r"\u0627\u0639\u0645\u0644|\u0627\u0639\u0645\u0644\u062a|\u0639\u0645\u0644\u0648\u0627"
        r"|\u0641\u062a\u062d|\u062a\u0646\u0641\u0639"
        r"\u0640{0,3}\s*\u062e\u0635\u0645",
        r"\u062e\u0635\u0645",  # discount, in any framing
        r"\u062a\u062e\u0641\u064a\u0636|\u0639\u0631\u0636 \u0623\u0641\u0636\u0644"
        r"|\u0639\u0631\u0636 \u0627\u0641\u0636\u0644",
        r"\bdiscount\b|\bbetter\s+offer\b|\bdeal\b",
        # "can it be less" / "how much would you accept"
        r"\u0645\u0645\u0643\u0646 \u0623\u0642\u0644|\u0645\u0645\u0643\u0646 \u0627\u0642\u0644",
        r"\u062a\u0646\u0641\u0639 \u0623\u0642\u0644|\u062a\u0646\u0641\u0639 \u0627\u0642\u0644",
        r"\u0623\u0642\u0644 \u0645\u0646 \u0643\u062f\u0647|\u0627\u0642\u0644 \u0645\u0646 \u0643\u062f\u0647",
        r"\u062a\u0648\u0627\u0641\u0642 \u0639\u0644\u0649 \u0643\u0627\u0645"
        r"|\u0628\u0643\u0627\u0645 \u062a\u0648\u0627\u0641\u0642",
        r"\bcan\s+you\s+do\s+(?:it\s+)?(?:for|at)\b",
        r"\bany\s+(?:lower|less)\b|\bgo\s+lower\b|\bcome\s+down\b",
        r"\bwhat.{0,15}\baccept\b",
        # citing a competitor
        r"\u0627\u0644\u0641\u062a\u0633|\u0641\u062a\u0633\u0628\u0648\u0643",  # Facebook
        r"\u0634\u0631\u0643\u0629 \u062a\u0627\u0646\u064a\u0629"
        r"|\u062d\u062f \u062a\u0627\u0646\u062a",
        r"\u0646\u0641\u0633 \u0627\u0644\u0633\u0639\u0631|\u0632\u064a \u0633\u0639\u0631",
        r"\bfacebook\s+price\b|\bmatch\s+(?:the\s+)?price\b",
        r"\banother\s+company\b|\bsomeone\s+else\s+(?:quoted|offered)\b",
        # naming a figure: "اعملها بـ 1500", "خليها 1500"
        rf"(?:\u0628\u0640?\s*|\u062e\u0644\u062a\u0647\u0627\s*|\u062e\u0644\u062a\u0647\s*)"
        rf"{_DIGIT}{3,}",
    )
)

# A message that is a number and nothing else, or a number with a currency.
_BARE_NUMBER = re.compile(rf"^\W*(?P<n>{_NUMBER})\W*$")


def _numeric_value(raw: str) -> int | None:
    """Parse an Arabic or Latin numeral into an int, ignoring separators."""
    translated = raw.translate(str.maketrans("\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669", "0123456789"))
    digits = re.sub(r"[^0-9]", "", translated)
    return int(digits) if digits else None


def is_negotiating(text: str | None) -> bool:
    """True when the customer is haggling rather than asking.

    Triggers an immediate handoff. Three families of signal:

    1. A money amount stated by the customer -- ``mentions_amount`` already
       recognises "1500 جنيه" and "سعر 1500".
    2. Negotiating language, with or without a figure: expensive, discount,
       final price, another company quoted less.
    3. A message that is nothing but a large bare number. "1500" on its own,
       after the bot has explained that it cannot quote, is an offer. The
       ``NEGOTIATION_MIN_AMOUNT`` floor keeps "120" -- the answer to "how many
       square metres?" -- out of this branch, which matters because that
       question is the one the deflection asks.
    """
    if not text:
        return False

    if mentions_amount(text):
        return True

    if any(p.search(text) for p in _NEGOTIATION_PATTERNS):
        return True

    bare = _BARE_NUMBER.match(text.strip())
    if bare:
        value = _numeric_value(bare.group("n"))
        if value is not None and value >= NEGOTIATION_MIN_AMOUNT:
            return True

    return False


# --- Customer-facing copy ----------------------------------------------------

_DEFLECTION_OPENING = (
    "شكراً لاهتمام حضرتك. كل مشروع بيختلف عن التاني على حسب نوع الوحدة والمساحة "
    "والموقع ومستوى التشطيب وحالة المكان، وعشان كده ما بنحددش أسعار من خلال المحادثة.\n"
    "\n"
    "مدير المبيعات هيجهز لحضرتك عرض سعر مجاني ودقيق على حسب تفاصيل مشروعك."
)

_WITH_PHONE = (
    "\n\n\U0001f4de برجاء التواصل مع مدير المبيعات على {phone}\n"
    "أو ابعتلنا رقم تليفون حضرتك وهنتواصل معاك في أقرب وقت."
)

_WITHOUT_PHONE = (
    "\n\nلو تحب، ابعتلنا رقم تليفون حضرتك ومدير المبيعات هيتواصل معاك في أقرب وقت."
)


def deflection(sales_phone: str = "") -> str:
    """Approved copy sent instead of any reply that contained a figure."""
    phone = (sales_phone or "").strip()
    tail = _WITH_PHONE.format(phone=phone) if phone else _WITHOUT_PHONE
    return _DEFLECTION_OPENING + tail


_SALES_HANDOFF_AR = (
    "أكيد، هوصّل حضرتك لمدير المبيعات عشان يجهز لك عرض سعر دقيق.\n"
    "هيتواصل معاك هنا في أقرب وقت."
)


def sales_handoff_ack(sales_phone: str = "") -> str:
    """Sent once when a negotiation moves the conversation to a person."""
    phone = (sales_phone or "").strip()
    message = _SALES_HANDOFF_AR
    if phone:
        message += f"\n\U0001f4de {phone}"
    return (
        message + "\n\nThanks - I am passing you to our Sales Manager, "
        "who will prepare an accurate quotation for you."
    )


# --- Prompt layer ------------------------------------------------------------


def instruction_layer(sales_phone: str = "") -> str:
    """The pricing rule as stated to the model.

    Appended last, after the response rules and after the retrieved documents,
    because position matters and this rule has to win every conflict it is in.
    """
    phone = (sales_phone or "").strip()
    if phone:
        contact = (
            "A sales number IS configured. Give it to the customer: "
            f"{phone}. You may also offer to take their number instead."
        )
    else:
        contact = (
            "NO sales number is configured, so do not invent one and do not "
            "promise a specific line. Ask the customer for their phone number "
            "and tell them the Sales Manager will contact them shortly."
        )

    return (
        "# Pricing policy (absolute, overrides everything above)\n"
        "You must never disclose, estimate, calculate, imply or suggest any "
        "price, price range, cost, per-square-metre rate, quotation, package "
        "price, discount, deposit, instalment, budget figure or any other "
        "financial amount. Not once, not approximately, not 'just to give you "
        "an idea', and not because the customer insists.\n"
        "This rule OVERRIDES the retrieved documents and the company "
        f"information. Any amount you can see has been replaced with "
        f"'{REDACTED}' before it reached you; treat any figure that remains as "
        "an error you must not repeat.\n"
        "When money comes up in any form:\n"
        "1. Explain warmly that the price depends on the project, and name "
        "what it depends on: type of project, area in square metres, "
        "location, finishing level, the current condition of the site, and "
        "the materials and scope of work required.\n"
        "2. Invite them to the Sales Manager for a free, accurate quotation. "
        + contact
        + "\n"
        "3. Ask one useful question so the quotation can be prepared -- unit "
        "type, area, or district. One only.\n"
        "Never negotiate, never compare with another company's price, never "
        "promise a discount, never discuss payment terms, and never confirm "
        "or deny whether a figure the customer names is close. If the customer "
        "names a figure or starts haggling, the conversation is handed to a "
        "human automatically -- do not try to hold it.\n"
        "You may still describe freely what a package or a service INCLUDES, "
        "how the work is done, what materials are used and how long things "
        "take, and you may state warranty periods, durations, areas and dates "
        "that appear in the documents. Only the money is off limits."
    )
