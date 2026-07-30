"""Tests for the absolute no-price rule.

The detection tests are grouped by which direction a failure goes:

* ``mentions_amount`` false negatives are the expensive failure -- a figure
  reaching a customer -- so that group is deliberately long and includes the
  awkward forms: Arabic-Indic digits, thousands words, percentages, currency
  before and after the number.
* ``mentions_amount`` false positives are the cheap failure, but not free, so
  the second group pins the cases that must stay clean: measurements, room
  counts, durations, and the approved copy itself.
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
        "$1500",
        "1500 USD",
        "€2000",
        "the cost is 1500 pounds",
        "حوالي 50 الف",
        "حوالي 50 ألف",
        "حوالي 2 مليون",
        "about 50k",
        "خصم 10%",
        "10% discount",
        "السعر تقريبا 1500",
        "price is around 1500",
        "the cost would be 90000",
        "المقدم 25000",
    ],
)
def test_amounts_are_detected(text: str) -> None:
    assert price_policy.mentions_amount(text) is True


# --- Text that must NOT trip the detector ------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "الشقة 120 متر مربع",
        "الشقة فيها 3 غرف",
        "التشطيب بياخد 45 يوم",
        "الضمان لمدة 5 سنوات",
        "the work takes 45 days",
        "we have 3 branches",
        "الدهان بيجف في 4 ساعات",
        "السعر بيعتمد على المساحة ومستوى التشطيب",
        "pricing depends on the area and the finishing level",
        "",
    ],
)
def test_ordinary_text_is_not_an_amount(text: str) -> None:
    assert price_policy.mentions_amount(text) is False


def test_none_is_not_an_amount() -> None:
    assert price_policy.mentions_amount(None) is False


# --- The approved copy must survive its own gate -----------------------------


def test_deflection_with_phone_does_not_trip_the_detector() -> None:
    """The message sent *because* of the rule must not violate the rule.

    A phone number is a long run of digits, so without the exemption the
    deflection would be replaced by itself forever.
    """
    phone = "01000000000"
    message = price_policy.deflection(phone)
    assert price_policy.mentions_amount(message, phone) is False


def test_deflection_without_phone_is_clean() -> None:
    message = price_policy.deflection("")
    assert price_policy.mentions_amount(message) is False


def test_sales_handoff_ack_survives_its_own_gate() -> None:
    phone = "+20 100 000 0000"
    ack = price_policy.sales_handoff_ack(phone)
    assert price_policy.mentions_amount(ack, phone) is False


def test_deflection_includes_the_phone_when_configured() -> None:
    assert "01000000000" in price_policy.deflection("01000000000")


def test_deflection_asks_for_a_number_when_none_configured() -> None:
    message = price_policy.deflection("")
    # No invented number, and an explicit request for theirs.
    assert "رقم تليفون" in message


def test_deflection_names_the_factors_the_policy_requires() -> None:
    message = price_policy.deflection("")
    for factor in ("المساحة", "الموقع", "التشطيب"):
        assert factor in message


# --- Redaction ---------------------------------------------------------------


def test_redact_removes_the_figure_but_keeps_the_prose() -> None:
    chunk = "باقة السوبر لوكس تشمل دهانات جوتن وسعر المتر 2500 جنيه شامل الخامات"
    redacted = price_policy.redact(chunk)
    assert "2500" not in redacted
    assert price_policy.REDACTED in redacted
    # The useful part of the document is still there.
    assert "جوتن" in redacted
    assert "الخامات" in redacted


def test_redacted_chunk_is_clean_by_the_output_gate() -> None:
    """Redaction and detection must agree, or the two layers fight."""
    chunk = "المتر 2500 جنيه والخصم 10% والمقدم 50 الف"
    assert price_policy.mentions_amount(price_policy.redact(chunk)) is False


def test_redact_leaves_measurements_alone() -> None:
    chunk = "الشقة 120 متر مربع وفيها 3 غرف والتنفيذ 45 يوم"
    assert price_policy.redact(chunk) == chunk


# --- Question detection (used only for counting) -----------------------------


@pytest.mark.parametrize(
    "text",
    [
        "السعر كام؟",
        "المتر بكام؟",
        "عايز أعرف الأسعار",
        "التكلفة إيه؟",
        "في خصم؟",
        "ممكن تقسيط؟",
        "ميزانيتي محدودة",
        "عايز عرض سعر",
        "how much does it cost?",
        "what is your pricing?",
        "can I get a quotation?",
        "any discount?",
        "is it expensive?",
        "what is the price per square meter?",
        "do you offer installments?",
    ],
)
def test_price_questions_are_recognised(text: str) -> None:
    assert price_policy.asks_about_price(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "عايز أعرف الخدمات اللي بتقدموها",
        "التشطيب بياخد قد إيه؟",
        "فين فروعكم؟",
        "عايز معاينة",
        "what materials do you use?",
        "where are you located?",
        "",
    ],
)
def test_non_price_questions_are_ignored(text: str) -> None:
    assert price_policy.asks_about_price(text) is False


# --- The insistence threshold ------------------------------------------------


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _bot(text: str) -> dict:
    return {"role": "assistant", "content": text}


def test_count_ignores_the_assistants_own_messages() -> None:
    """The deflection itself contains the word 'price'.

    Counting assistant turns would mean one customer question plus two
    deflections crosses the threshold on its own.
    """
    history = [
        _user("السعر كام؟"),
        _bot(price_policy.deflection("")),
        _bot(price_policy.deflection("")),
    ]
    assert price_policy.count_price_asks(history) == 1


def test_threshold_is_not_met_on_a_first_ask() -> None:
    history = [_user("السعر كام؟")]
    assert price_policy.count_price_asks(history) < price_policy.INSIST_THRESHOLD


def test_threshold_is_met_on_the_third_ask() -> None:
    history = [
        _user("السعر كام؟"),
        _bot("..."),
        _user("طيب تقريبي المتر بكام؟"),
        _bot("..."),
        _user("يا أخي قولي التكلفة بس"),
    ]
    assert price_policy.count_price_asks(history) >= price_policy.INSIST_THRESHOLD


def test_unrelated_questions_do_not_accumulate() -> None:
    history = [
        _user("السعر كام؟"),
        _bot("..."),
        _user("إيه الخامات اللي بتستخدموها؟"),
        _bot("..."),
        _user("والتنفيذ بياخد قد إيه؟"),
    ]
    assert price_policy.count_price_asks(history) == 1


def test_count_tolerates_missing_content() -> None:
    assert price_policy.count_price_asks([{"role": "user"}]) == 0


# --- The prompt layer --------------------------------------------------------


def test_instruction_layer_gives_the_number_when_configured() -> None:
    layer = price_policy.instruction_layer("01000000000")
    assert "01000000000" in layer
    assert "A sales number IS configured" in layer


def test_instruction_layer_forbids_inventing_one_when_unset() -> None:
    layer = price_policy.instruction_layer("")
    assert "do not invent one" in layer


def test_instruction_layer_states_that_it_overrides_documents() -> None:
    layer = price_policy.instruction_layer("")
    assert "OVERRIDES" in layer


def test_instruction_layer_still_allows_describing_scope() -> None:
    """The rule closes money, not the whole conversation."""
    layer = price_policy.instruction_layer("")
    assert "INCLUDES" in layer
