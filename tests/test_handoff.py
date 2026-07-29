"""Human handoff: the bot must go silent once a person owns the conversation.

The expensive failure here is not a missed keyword -- it is the bot answering
over an operator, or answering someone who explicitly asked for a human. Both
are covered against the real database and the real webhook entry point.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.conversation import MODE_BOT, MODE_HUMAN
from app.repositories.conversation import ConversationRepository
from app.services.handoff import wants_human
from app.services.webhook_processor import process_webhook_payload
from tests.conftest import Customer
from tests.test_webhook_integration import FakeOpenAI, FakeWhatsApp

# Arabic is written as \u escapes for the same reason as in the module under
# test: the file stays pure ASCII. Transliterations are in the comments.
ASKS_FOR_A_HUMAN = (
    "I want to speak to a representative",
    "Can someone call me?",
    "I want the manager",
    "I don't want to interact with the bot",
    "Please transfer me to customer service",
    "Is there a real person there?",
    # aayez modeer - I want a manager
    "\u0639\u0627\u064a\u0632 \u0645\u062f\u064a\u0631",
    # momken mowazzaf - can an employee ...
    "\u0645\u0645\u0643\u0646 \u0645\u0648\u0638\u0641",
    # mesh aayez bot - I don't want a bot
    "\u0645\u0634 \u0639\u0627\u064a\u0632 \u0628\u0648\u062a",
)

ORDINARY_MESSAGES = (
    "What time do you open?",
    "How much does gypsum board cost per meter?",
    # "manager" alone must not trigger: this is a normal question for a
    # finishing business, and a false positive silences the bot.
    "Do you have a project manager for site visits?",
    "Thanks, that answers my question",
    # kam se'r el-meter - how much per meter
    "\u0643\u0645 \u0633\u0639\u0631 \u0627\u0644\u0645\u062a\u0631",
    "",
)


@pytest.mark.parametrize("message", ASKS_FOR_A_HUMAN)
def test_requests_for_a_person_are_detected(message: str) -> None:
    assert wants_human(message)


@pytest.mark.parametrize("message", ORDINARY_MESSAGES)
def test_ordinary_messages_do_not_trigger_a_handoff(message: str) -> None:
    assert not wants_human(message)


def test_a_missing_caption_is_not_a_request() -> None:
    """Media captions are passed straight through, so None is a normal input."""
    assert not wants_human(None)


def _payload(wa_id: str, wa_message_id: str, body: str) -> dict[str, Any]:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [
                                {"wa_id": wa_id, "profile": {"name": "Test"}}
                            ],
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


async def _mode(session: AsyncSession, conversation_id: int) -> str | None:
    return await session.scalar(
        text("SELECT mode FROM conversations WHERE id = :id"),
        {"id": conversation_id},
    )


async def test_no_model_call_while_an_operator_owns_the_conversation(
    db: AsyncSession, customer: Customer
) -> None:
    """The whole point: OpenAI is never called, but the message is still kept."""
    conversations = ConversationRepository(db)
    conversation = await conversations.get(customer.conversation_id)
    assert conversation is not None
    await conversations.set_mode(conversation, MODE_HUMAN, operator="Ahmed")
    await db.commit()

    ai = FakeOpenAI()
    whatsapp = FakeWhatsApp()
    await process_webhook_payload(
        db,
        whatsapp,
        ai,
        get_settings(),
        _payload(customer.wa_id, "wamid.handoff.1", "Are you there?"),
    )

    assert ai.calls == []
    assert whatsapp.sent == []
    # Stored and marked read even though nobody answered it.
    assert whatsapp.read == ["wamid.handoff.1"]
    stored = await db.scalar(
        text(
            "SELECT count(*) FROM messages "
            "WHERE conversation_id = :id AND direction = 'inbound'"
        ),
        {"id": customer.conversation_id},
    )
    assert stored == 1


async def test_asking_for_a_person_stops_the_bot_and_acknowledges_once(
    db: AsyncSession, customer: Customer
) -> None:
    ai = FakeOpenAI()
    whatsapp = FakeWhatsApp()
    await process_webhook_payload(
        db,
        whatsapp,
        ai,
        get_settings(),
        _payload(
            customer.wa_id,
            "wamid.handoff.2",
            "I want to speak to a representative",
        ),
    )

    assert ai.calls == []
    # Exactly one message out: the acknowledgement, not an AI answer.
    assert len(whatsapp.sent) == 1
    assert await _mode(db, customer.conversation_id) == MODE_HUMAN

    # A follow-up message is now left entirely alone.
    await process_webhook_payload(
        db,
        whatsapp,
        ai,
        get_settings(),
        _payload(customer.wa_id, "wamid.handoff.3", "Hello?"),
    )
    assert ai.calls == []
    assert len(whatsapp.sent) == 1


async def test_the_bot_answers_again_after_the_ai_is_resumed(
    db: AsyncSession, customer: Customer
) -> None:
    conversations = ConversationRepository(db)
    conversation = await conversations.get(customer.conversation_id)
    assert conversation is not None
    await conversations.set_mode(conversation, MODE_HUMAN, operator="Ahmed")
    await db.commit()
    await conversations.set_mode(conversation, MODE_BOT)
    await db.commit()

    assert conversation.assigned_operator is None
    assert conversation.handoff_at is None

    ai = FakeOpenAI()
    whatsapp = FakeWhatsApp()
    await process_webhook_payload(
        db,
        whatsapp,
        ai,
        get_settings(),
        _payload(customer.wa_id, "wamid.handoff.4", "What time do you open?"),
    )
    assert len(ai.calls) == 1
    assert len(whatsapp.sent) == 1


def test_takeover_and_resume_through_the_api(
    client: TestClient, admin_headers: dict[str, str], sync_customer: Customer
) -> None:
    base = f"/admin/conversations/{sync_customer.conversation_id}"

    taken = client.post(
        f"{base}/takeover", headers=admin_headers, json={"operator": "Ahmed"}
    )
    assert taken.status_code == 200
    body = taken.json()
    assert body["mode"] == MODE_HUMAN
    assert body["assigned_operator"] == "Ahmed"
    assert body["handoff_at"] is not None
    # Lifecycle is untouched: the conversation is still the customer's open one.
    assert body["status"] == "active"

    resumed = client.post(f"{base}/resume-ai", headers=admin_headers)
    assert resumed.status_code == 200
    body = resumed.json()
    assert body["mode"] == MODE_BOT
    assert body["assigned_operator"] is None
    assert body["handoff_at"] is None


def test_takeover_requires_the_admin_key(
    client: TestClient, sync_customer: Customer
) -> None:
    response = client.post(
        f"/admin/conversations/{sync_customer.conversation_id}/takeover"
    )
    assert response.status_code == 401


def test_takeover_of_a_missing_conversation_is_404(
    client: TestClient, admin_headers: dict[str, str], requires_database: None
) -> None:
    response = client.post(
        "/admin/conversations/99999999/takeover", headers=admin_headers
    )
    assert response.status_code == 404
