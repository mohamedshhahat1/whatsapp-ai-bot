"""The welcome is approved company copy, so its guarantees are tested.

"Always start with this welcome, and never repeat it" is a counting rule. A
prompt cannot enforce it; the code prepends the text once, decided by a
database count. These tests are what keep that true, and they also pin the two
places where the persona must stay honest about what the bot can actually do.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.services.persona import (
    NOT_UNDERSTOOD,
    SYSTEM_PROMPT,
    WELCOME,
    is_unintelligible,
)
from app.services.prompt_builder import PromptBuilder
from app.services.webhook_processor import process_webhook_payload
from tests.conftest import new_wa_id, purge
from tests.test_webhook_integration import REPLY, FakeOpenAI, FakeWhatsApp

NO_WORDS = (".", "...", "؟", "\U0001f44d", "\u2764\ufe0f", "!!", "   ", "")
REAL_REQUESTS = (
    "عايز أعرف سعر تشطيب شقة",
    "مرحبا",
    "Hello",
    "120",
)


def _payload(wa_id: str, wa_message_id: str, body: str) -> dict[str, Any]:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": wa_id, "profile": {"name": "Test"}}],
                            "messages": [
                                {
                                    "from": wa_id,
                                    "id": wa_message_id,
                                    "type": "text",
                                    "text": {"body": body},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


def test_messages_without_letters_or_digits_are_unintelligible() -> None:
    for text in NO_WORDS:
        assert is_unintelligible(text), text
    assert is_unintelligible(None)


def test_anything_with_words_is_left_to_the_model() -> None:
    """Only emptiness is decided in code; judging meaning is the model's job."""
    for text in REAL_REQUESTS:
        assert not is_unintelligible(text), text


def test_the_packaged_persona_is_used_when_nothing_overrides_it() -> None:
    instructions = PromptBuilder(Settings(system_prompt="")).build_instructions()
    assert SYSTEM_PROMPT.strip() in instructions


def test_system_prompt_still_overrides_the_persona() -> None:
    """Another business must be able to reuse this without editing Python."""
    settings = Settings(system_prompt="You are a laconic assistant.")
    instructions = PromptBuilder(settings).build_instructions()
    assert "You are a laconic assistant." in instructions
    assert "El Kayan" not in instructions


def test_the_persona_never_claims_to_see_photos_or_read_files() -> None:
    """Regression: the media path sends no image, only a placeholder.

    If someone re-adds "analyse what is visible", the bot starts describing
    photos it never received, confidently and wrongly.
    """
    assert "CANNOT see images" in SYSTEM_PROMPT
    assert "never claim to have read a document" in SYSTEM_PROMPT.lower()


def test_the_model_is_told_the_welcome_was_already_sent() -> None:
    builder = PromptBuilder(Settings(system_prompt=""))
    first = builder.build_instructions(is_first_message=True)
    later = builder.build_instructions()
    assert "ALREADY been prepended" in first
    assert "ALREADY been prepended" not in later
    # The rule against greeting applies on every turn, not just the first.
    assert "Never write a welcome" in later


async def test_the_welcome_is_sent_once_and_only_on_the_first_message(
    db: AsyncSession,
) -> None:
    wa_id = new_wa_id()
    whatsapp = FakeWhatsApp()
    ai = FakeOpenAI()

    try:
        await process_webhook_payload(
            db,
            whatsapp,
            ai,
            get_settings(),
            _payload(wa_id, f"wamid.in.{wa_id}.1", "عايز أعرف سعر تشطيب شقة"),
        )
        await process_webhook_payload(
            db,
            whatsapp,
            ai,
            get_settings(),
            _payload(wa_id, f"wamid.in.{wa_id}.2", "والفيلا؟"),
        )

        assert len(whatsapp.sent) == 2
        assert whatsapp.sent[0] == (wa_id, f"{WELCOME}\n\n{REPLY}")
        # The second answer carries no welcome at all.
        assert whatsapp.sent[1] == (wa_id, REPLY)
    finally:
        await purge(db, wa_id)


async def test_an_opening_message_with_no_words_skips_the_model(
    db: AsyncSession,
) -> None:
    """A thumbs-up costs no tokens and still gets the approved wording."""
    wa_id = new_wa_id()
    whatsapp = FakeWhatsApp()
    ai = FakeOpenAI()

    try:
        await process_webhook_payload(
            db,
            whatsapp,
            ai,
            get_settings(),
            _payload(wa_id, f"wamid.in.{wa_id}", "\U0001f44d"),
        )

        assert ai.calls == []
        assert whatsapp.sent == [(wa_id, f"{WELCOME}\n\n{NOT_UNDERSTOOD}")]
    finally:
        await purge(db, wa_id)
