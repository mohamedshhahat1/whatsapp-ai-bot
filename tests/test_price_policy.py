"""Tests for the absolute no-price rule.

Three groups, each pinning a different failure:

* ``mentions_amount`` false negatives -- a figure reaching a customer. The
  expensive one.
* ``redact`` false positives -- a warranty period or a founding year turned
  into a placeholder. This one is quiet: the bot keeps answering, just worse,
  and nobody notices until a customer is told the company cannot say how long
  the guarantee lasts.
* ``is_negotiating`` in both directions -- escalating a customer who was
  answering a qualifying question, or failing to escalate one who is haggling.
"""

import pytest

from app.services import price_policy


# --- Amounts that MUST be caught ---------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "المتر 1500 جنيه",
        "المتر ١٥٠٠ جنيه",
        "التكلفة حوالي 250,000 جنيه",
        "السعر جنيه 1500",
        "المتر بـ 1500 ج.م",
        "around 1500 EGP per metre",
        "EGP 1500",
        "1500 SAR",
        "$1500",
        "1500 USD",
        "€2000",
        "the cost is 1500 pounds",
        "سعر المتر 2500",
        "السعر تقريبا 1500",
        "price is around 1500",
        "the cost would be 90000",
        "المقدم 25000",
        "2500 للمتر",
        "2500 per square metre",
        "1500/m²",
        "حوالي 50 ألف",
        "حوالي 2 مليون",
        "about 50k",
        "خصم 10%",
        "10% discount",
    ],
)
def test_amounts_are_detected(text: str) -> None:
    assert price_policy.mentions_amount(text) is True


# --- Numbers that are NOT money ----------------------------------------------
# Every one of these appears in a real finishing knowledge base. Redacting any
# of them removes an answer the bot is supposed to give.


NON_MONEY = [
    "فترة الضمان 10 سنوات",
    "الضمان لمدة 5 سنة",
    "التنفيذ 45 يوم",
    "المدة حوالي 3 أشهر",
    "تأسست الشركة عام 2018",
    "الشقة 120 متر مربع",
    "الفيلا 450 متر",
    "الشقة فيها 3 غرف و 2 حمام",
    "بنحط 3 طبقات دهان",
    "الدهان بيجف في 4 ساعات",
    "السمك 12 مم",
    "the warranty is 10 years",
    "execution takes 45 days",
    "founded in 2018",
    "the apartment is 120 square metres",
    "we have 3 branches",
    "السعر بيعتمد على المساحة ومستوى التشطيب",
    "pricing depends on the area and the finishing level",
    "الرطوبة لازم تكون أقل من 60%",
]


@pytest.mark.parametrize("text", NON_MONEY)
def test_non_money_numbers_are_not_amounts(text: str) -> None:
    assert price_policy.mentions_amount(text) is False


@pytest.mark.parametrize("text", NON_MONEY)
def test_redaction_leaves_non_money_untouched(text: str) -> None:
    """The knowledge base must survive the trip into the prompt intact."""
    assert price_policy.redact(text) == text


def test_none_is_not_an_amount() -> None:
    assert price_policy.mentions_amount(None) is False


# --- Redaction keeps the sentence readable -----------------------------------


def test_redact_hides_the_figure_and_keeps_the_words() -> None:
    chunk = "سعر المتر 2500 جنيه شامل الخامات"
    redacted = price_policy.redact(chunk)
    assert "2500" not in redacted
    assert price_policy.REDACTED in redacted
    assert "سعر المتر" in redacted
    assert "شامل الخامات" in redacted


def test_redact_handles_a_mixed_paragraph() -> None:
    """A realistic chunk: money removed, everything else preserved."""
    chunk = (
        "باقة السوبر لوكس تشمل دهانات جوتن وسعر المتر 2500 جنيه. "
        "مدة التنفيذ 60 يوم والضمان 10 سنوات."
    )
    redacted = price_policy.redact(chunk)
    assert "2500" not in redacted
    assert "60 يوم" in redacted
    assert "10 سنوات" in redacted
    assert "جوتن" in redacted


def test_redacted_chunk_is_clean_by_the_output_gate() -> None:
    """Redaction and detection must agree, or the two layers fight."""
    chunk = "المتر 2500 جنيه والخصم 10% والمقدم 50 ألف"
    assert price_policy.mentions_amount(price_policy.redact(chunk)) is False


def test_thousands_with_a_unit_is_a_measurement() -> None:
    assert price_policy.redact("المشروع 50 ألف متر") == "المشروع 50 ألف متر"


# --- The approved copy must survive its own gate -----------------------------


def test_deflection_with_phone_does_not_trip_the_detector() -> None:
    phone = "01000000000"
    assert price_policy.mentions_amount(price_policy.deflection(phone), phone) is False


def test_deflection_without_phone_is_clean() -> None:
    assert price_policy.mentions_amount(price_policy.deflection("")) is False


def test_sales_handoff_ack_survives_its_own_gate() -> None:
    phone = "+20 100 000 0000"
    assert price_policy.mentions_amount(price_policy.sales_handoff_ack(phone), phone) is False


def test_deflection_includes_the_phone_when_configured() -> None:
    assert "01000000000" in price_policy.deflection("01000000000")


def test_deflection_asks_for_a_number_when_none_configured() -> None:
    assert "رقم تليفون" in price_policy.deflection("")


# --- Asking (deflect, do NOT escalate) ---------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "سعر الجبس كام؟",
        "المتر بكام؟",
        "عايز أعرف الأسعار",
        "how much does gypsum cost?",
        "what is your pricing?",
        "can I get a quotation?",
    ],
)
def test_plain_questions_are_asks_not_negotiations(text: str) -> None:
    """The most common opening question in the business.

    It must reach the model and get the deflection. Escalating here would put
    a human on the other end of nearly every new conversation.
    """
    assert price_policy.asks_about_price(text) is True
    assert price_policy.is_negotiating(text) is False


# --- Negotiating (escalate immediately) --------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "طيب اعملها بـ 1500",
        "خليها 2000 وخلاص",
        "1500",
        "٢٠٠٠",
        "1500 جنيه",
        "ده غالي أوي",
        "غالي",
        "that's expensive",
        "إيه آخر سعر؟",
        "السعر النهائي كام؟",
        "what's your final price?",
        "give me your best price",
        "اعمللي خصم",
        "فيه خصم؟",
        "any discount?",
        "ممكن أقل من كده؟",
        "ينفع أقل؟",
        "can you do it for less",
        "can you go lower?",
        "أنا شفت سعر على الفيس أقل",
        "facebook price is cheaper",
        "can you match the price?",
        "another company offered better",
        "عرض أفضل موجود",
    ],
)
def test_negotiation_escalates(text: str) -> None:
    assert price_policy.is_negotiating(text) is True


# --- Negotiation false positives, which break qualification ------------------


@pytest.mark.parametrize(
    "text",
    [
        "120",  # answer to "how many square metres?"
        "85",
        "3",  # answer to "how many rooms?"
        "١٢٠",
        "الشقة 120 متر",
        "التنفيذ 45 يوم؟",
        "الضمان 10 سنوات؟",
        "عايز معاينة",
        "فين فروعكم؟",
        "what materials do you use?",
        "",
    ],
)
def test_ordinary_answers_do_not_escalate(text: str) -> None:
    """The deflection asks for area and unit type.

    If a two- or three-digit reply escalated, the bot would hand every
    customer to sales at the exact moment they cooperated with it.
    """
    assert price_policy.is_negotiating(text) is False


def test_none_does_not_escalate() -> None:
    assert price_policy.is_negotiating(None) is False


# --- The prompt layer --------------------------------------------------------


def test_instruction_layer_gives_the_number_when_configured() -> None:
    layer = price_policy.instruction_layer("01000000000")
    assert "01000000000" in layer


def test_instruction_layer_forbids_inventing_one_when_unset() -> None:
    assert "do not invent one" in price_policy.instruction_layer("")


def test_instruction_layer_states_that_it_overrides_documents() -> None:
    assert "OVERRIDES" in price_policy.instruction_layer("")


def test_instruction_layer_permits_durations_and_warranties() -> None:
    """The model must know that only money is closed, not every number."""
    layer = price_policy.instruction_layer("")
    assert "warranty periods, durations, areas and dates" in layer
