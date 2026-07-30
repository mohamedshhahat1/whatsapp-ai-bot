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
  told to ignore the one thing it was told to trust. That conflict resolves
  the wrong way often enough to matter.
* Persistence. A customer who asks five times, reframes it as "just roughly",
  or claims a competitor quoted them, is running the same pressure that works
  on people.
* ``SYSTEM_PROMPT``. It replaces the packaged persona wholesale, so a persona
  paragraph can be configured away by accident.

So the rule is enforced at four points, of which only one is a prompt:

1. ``redact`` strips amounts from retrieved chunks before the prompt is built.
   The model cannot repeat a figure it was never shown.
2. ``instruction_layer`` is appended last, after the response rules, and says
   plainly that it outranks the documents.
3. ``mentions_amount`` scans the generated reply. A reply containing an amount
   is thrown away and replaced with ``deflection`` -- the model does not get a
   second chance to phrase it better.
4. ``INSIST_THRESHOLD`` counts how many times the customer has raised money.
   On the third, the conversation goes to a human sales representative.

Layer 3 is the one that actually holds the line, and it is deliberately blunt:
it does not edit the number out of an otherwise good reply, because a sentence
with its figure removed reads as evasive and often leaves the number implied
by context ("that would be about that per metre"). Replacing the whole message
with approved copy is the only version that cannot leak.

The cost of layer 3 is false positives: a legitimate reply mentioning "100
metres" is safe, but one saying "the warranty covers 5 years" near a currency
word is not, and gets replaced by a pricing deflection that answers a question
nobody asked. That trade is deliberate. A deflection sent in error costs one
awkward message; a figure sent in error is a number the company never agreed
to, in writing, on the customer's phone.

This file is written in real Arabic rather than \\uXXXX escapes because it
contains copy that customers read, and escaped codepoints cannot be proofread
by the person who owns the wording.
"""

import re

# Reason recorded on the conversation when pricing pressure forces a handoff.
SALES_HANDOFF_REASON = "customer_pressed_for_a_price"

# How many times a customer may raise money before a human takes the
# conversation. Three, not two: the first ask is ordinary, the second is often
# the customer rephrasing because the deflection was not clear, and the third
# is someone who is not going to accept the answer from a bot.
INSIST_THRESHOLD = 3

# What a redacted figure becomes inside a retrieved document. Visible rather
# than silent: the model should understand that a value was withheld, so it
# says "a colleague will quote that" instead of "the document does not say".
REDACTED = "[\u0633\u0639\u0631 \u0645\u062d\u062c\u0648\u0628]"

# --- Amount detection --------------------------------------------------------
# Arabic-Indic digits are included throughout: a model writing Arabic will
# sometimes produce ١٥٠٠ rather than 1500.
_DIGIT = r"[0-9\u0660-\u0669]"
_NUMBER = rf"{_DIGIT}[0-9\u0660-\u0669,\u066c\u066b\.\s]{{0,15}}"

# Currency names and symbols, Egyptian first.
_CURRENCY = (
    r"\u062c\u0646\u064a\u0647\u0627\u062a"  # gunayhaat
    r"|\u062c\u0646\u064a\u0647"  # gunayh
    r"|\u062c\u0646\u064a\u0629"  # gunayya (common misspelling)
    r"|\u062c\.?\s?\u0645\b"  # g.m
    r"|\u062f\u0648\u0644\u0627\u0631"  # dollar
    r"|\u064a\u0648\u0631\u0648"  # euro
    r"|\u0631\u064a\u0627\u0644"  # riyal
    r"|\u062f\u0631\u0647\u0645"  # dirham
    r"|EGP|USD|EUR|L\.?E\.?|\bLE\b|pounds?|dollars?|euros?"
)

# Multipliers that carry an amount without a currency word: "الف", "الفين",
# "مليون", "k". "المتر بخمستالاف" is a price even with no "جنيه" in it.
_MULTIPLIER = (
    r"\u0627\u0644\u0627\u0641"  # alaaf
    r"|\u0622\u0644\u0627\u0641"  # aalaaf
    r"|\u0627\u0644\u0641"  # alf
    r"|\u0623\u0644\u0641"  # alf
    r"|\u0645\u0644\u064a\u0648\u0646"  # million
)

_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # 1500 EGP / ١٥٠٠ جنيه / 1,500.00 pounds
        rf"{_NUMBER}\s*(?:{_CURRENCY})",
        # EGP 1500 / $1500 / جنيه ١٥٠٠
        rf"(?:{_CURRENCY})\s*{_NUMBER}",
        # $1500 and €1500 -- symbols, which have no word boundary
        rf"[$\u20ac\u00a3]\s*{_NUMBER}",
        rf"{_NUMBER}\s*[$\u20ac\u00a3]",
        # 50 الف / 2 مليون / 50k
        rf"{_NUMBER}\s*(?:{_MULTIPLIER})",
        rf"{_DIGIT}+\s*k\b",
        # A money word within a short distance of a digit: "السعر حوالي 1500",
        # "price is around 1500", "خصم 10%". Deliberately loose -- see the
        # module docstring on why a false positive is the cheap direction.
        rf"(?:\u0633\u0639\u0631|\u0627\u0644\u0633\u0639\u0631"
        rf"|\u062a\u0643\u0644\u0641\u0629|\u0627\u0644\u062a\u0643\u0644\u0641\u0629"
        rf"|\u062e\u0635\u0645|\u0645\u0642\u062f\u0645"
        rf"|price|cost|discount|deposit|quote)"
        rf"[^0-9\u0660-\u0669\n]{{0,20}}{_DIGIT}",
        # "10%" on its own is a discount in a pricing conversation.
        rf"{_DIGIT}+\s*%",
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
        # Also strip the digits-only form, since a model may reformat it.
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
    return any(p.search(_strip_phone(text, sales_phone)) for p in _PATTERNS)


def redact(content: str) -> str:
    """Replace every amount in a retrieved document with ``REDACTED``.

    Applied to knowledge-base chunks on their way into the prompt. The
    surrounding prose survives, so the model can still explain what a package
    includes and what a price depends on -- it simply never sees the figure.
    """
    cleaned = content
    for pattern in _PATTERNS:
        cleaned = pattern.sub(REDACTED, cleaned)
    return cleaned


# --- Question detection ------------------------------------------------------
# Used on the *customer's* messages, only to count how often money has come up.
# Nothing is blocked on the strength of this, so the list can be generous.

_ASK_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\u0633\u0639\u0631",  # se'r - price
        r"\u0627\u0633\u0639\u0627\u0631|\u0623\u0633\u0639\u0627\u0631",  # as'aar
        r"\u0628\u0643\u0627\u0645|\u0628\u0643\u0645",  # bekaam - how much
        r"\u062a\u0643\u0644\u0641|\u062a\u0643\u0627\u0644\u064a\u0641",  # taklif
        r"\u0645\u064a\u0632\u0627\u0646\u064a\u0629",  # mizaniyya - budget
        r"\u062e\u0635\u0645|\u062a\u062e\u0641\u064a\u0636",  # discount
        r"\u062a\u0642\u0633\u064a\u0637|\u0627\u0642\u0633\u0627\u0637|\u0623\u0642\u0633\u0627\u0637",  # instalments
        r"\u062f\u0641\u0639\u0629|\u0645\u0642\u062f\u0645",  # payment / deposit
        r"\u0639\u0631\u0636 \u0633\u0639\u0631",  # quotation
        r"\u063a\u0627\u0644\u064a|\u0631\u062e\u064a\u0635|\u0627\u0631\u062e\u0635|\u0623\u0631\u062e\u0635",  # expensive / cheap
        r"\u0641\u0644\u0648\u0633|\u0645\u0628\u0644\u063a",  # money / amount
        r"\bhow\s+much\b",
        r"\bpric\w*\b",
        r"\bcosts?\b|\bcosting\b",
        r"\bquot\w*\b",
        r"\bbudget\b",
        r"\bdiscount\w*\b",
        r"\binstal?lment\w*\b",
        r"\bdeposit\b",
        r"\bcheap\w*\b|\bexpensive\b|\bafford\w*\b",
        r"\bper\s+(?:square\s+)?met(?:er|re)\b",
    )
)


def asks_about_price(text: str | None) -> bool:
    """True when a customer message raises money in any form."""
    if not text:
        return False
    return any(p.search(text) for p in _ASK_PATTERNS)


def count_price_asks(history: list[dict]) -> int:
    """How many of the customer's messages in this history raise money."""
    return sum(
        1
        for message in history
        if message.get("role") == "user"
        and asks_about_price(str(message.get("content") or ""))
    )


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
    """Approved copy sent instead of any reply that contained a figure.

    Sent by the code, never generated, for the same reason as the welcome: a
    rule that must hold every single time cannot be delegated to a model.
    """
    phone = (sales_phone or "").strip()
    tail = _WITH_PHONE.format(phone=phone) if phone else _WITHOUT_PHONE
    return _DEFLECTION_OPENING + tail


_SALES_HANDOFF_AR = (
    "أكيد، هوصّل حضرتك لمدير المبيعات عشان يجهز لك عرض سعر دقيق.\n"
    "هيتواصل معاك هنا في أقرب وقت."
)


def sales_handoff_ack(sales_phone: str = "") -> str:
    """Sent once when pricing pressure moves the conversation to a person."""
    phone = (sales_phone or "").strip()
    message = _SALES_HANDOFF_AR
    if phone:
        message += f"\n\U0001f4de {phone}"
    return (
        message
        + "\n\nThanks - I am passing you to our Sales Manager, "
        "who will prepare an accurate quotation for you."
    )


# --- Prompt layer ------------------------------------------------------------


def instruction_layer(sales_phone: str = "") -> str:
    """The pricing rule as stated to the model.

    Appended last, after the response rules and after the retrieved documents,
    because position matters and this rule has to win every conflict it is in.
    It is belt-and-braces: ``redact`` has already removed the figures and
    ``mentions_amount`` will catch anything that gets through. What this layer
    buys is a *good* refusal rather than a replaced one -- a model that knows
    the rule explains it warmly and asks the right follow-up question, while a
    model that only meets the output gate produces a reply the code has to
    throw away.
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
        "or deny whether a figure the customer names is close. If they press "
        "you, do not soften: say a colleague will handle it.\n"
        "You may still describe freely what a package or a service INCLUDES, "
        "how the work is done, what materials are used and how long things "
        "take. Only the money is off limits."
    )
