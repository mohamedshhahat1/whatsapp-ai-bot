"""Unfinished documents must not become retrievable chunks.

The knowledge base is the bot's only source for company facts, and the
response rules permit a price the moment a matching document exists. A half
filled template therefore does not fail loudly -- it quietly becomes an
authoritative source. These tests pin the guard that prevents it.

The embedding client is never called here: the point of the guard is that
nothing reaches it.
"""

from pathlib import Path

import pytest

from app.services import knowledge_guard
from app.services.ingestion import read_pages

FILLED = """# أسعار التشطيب

سعر المتر للدهانات: 150 جنيه.
"""

UNFILLED = """# أسعار التشطيب

سعر المتر للدهانات: [[TODO]] جنيه.
مدة التنفيذ: [[TODO]] يوم.
"""


def test_a_finished_document_is_not_flagged() -> None:
    assert not knowledge_guard.is_unfilled(FILLED)


def test_a_template_is_flagged() -> None:
    assert knowledge_guard.is_unfilled(UNFILLED)


def test_every_remaining_slot_is_counted() -> None:
    """The count is what tells an owner how much work is left in a file."""
    assert knowledge_guard.count_placeholders(UNFILLED) == 2
    assert knowledge_guard.count_placeholders(FILLED) == 0


def test_the_reason_names_the_marker_and_the_count() -> None:
    reason = knowledge_guard.describe(UNFILLED)
    assert "2" in reason
    assert knowledge_guard.PLACEHOLDER_MARKER in reason


def test_one_remaining_slot_reads_as_singular() -> None:
    assert knowledge_guard.describe("a [[TODO]] b") == (
        "1 unfilled [[TODO]] placeholder"
    )


@pytest.mark.parametrize(
    "text",
    [
        "TODO: call the supplier",
        "[TODO]",
        "ملاحظة: TODO",
        "[[ TODO ]]",
    ],
)
def test_ordinary_prose_about_todos_is_not_a_placeholder(text: str) -> None:
    """Only the exact marker counts.

    Rejecting a real document is not a safe failure: it makes the bot say it
    has no information about the very thing the company wanted answered. So
    the check is deliberately literal rather than clever.
    """
    assert not knowledge_guard.is_unfilled(text)


def test_the_guard_sees_what_the_ingester_reads(tmp_path: Path) -> None:
    """The check runs on extracted text, so it must survive that round trip.

    A guard that inspected the raw file while ingestion embedded something else
    would be decorative.
    """
    path = tmp_path / "economy.md"
    path.write_text(UNFILLED, encoding="utf-8")

    pages = read_pages(path)
    extracted = "\n".join(text for _, text in pages)

    assert knowledge_guard.is_unfilled(extracted)
    assert knowledge_guard.count_placeholders(extracted) == 2
