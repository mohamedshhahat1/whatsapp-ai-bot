"""Regression: the closed 24-hour window is a 409, not a 500.

Meta rejects free-form messages more than 24 hours after the customer's last
message. The operator's request is valid, so returning 500 both misled the
operator and polluted the error rate that alerting watches.

These tests drive the HTTP layer, so all database work goes through run_db
rather than the async session fixture.
"""

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.repositories.message import MessageRepository
from app.services.reply_service import OutsideServiceWindowError
from tests.conftest import Customer, run_db

_BACKDATE = (
    "UPDATE messages SET created_at = now() - interval '30 hours' WHERE id = :id"
)


def test_outside_window_error_is_a_conflict() -> None:
    assert issubclass(OutsideServiceWindowError, ConflictError)
    assert OutsideServiceWindowError.status_code == 409


def test_reply_to_silent_customer_is_rejected(
    client: TestClient,
    admin_headers: dict[str, str],
    sync_customer: Customer,
) -> None:
    """A customer who has never written in cannot be messaged free-form."""
    response = client.post(
        f"/admin/conversations/{sync_customer.conversation_id}/reply",
        headers=admin_headers,
        json={"text": "Hello?"},
    )
    assert response.status_code == 409


def test_reply_after_the_window_closes_is_rejected(
    client: TestClient,
    admin_headers: dict[str, str],
    sync_customer: Customer,
) -> None:
    async def add_old_message(session: AsyncSession) -> None:
        message = await MessageRepository(session).create(
            conversation_id=sync_customer.conversation_id,
            direction="inbound",
            content="Are you open on Sunday?",
            wa_message_id=f"wamid.{sync_customer.wa_id}",
        )
        await session.execute(text(_BACKDATE), {"id": message.id})
        await session.commit()

    run_db(add_old_message)

    response = client.post(
        f"/admin/conversations/{sync_customer.conversation_id}/reply",
        headers=admin_headers,
        json={"text": "Sorry for the delay."},
    )
    assert response.status_code == 409
    assert "template" in response.text.lower()


def test_reply_to_unknown_conversation_is_404(
    client: TestClient, admin_headers: dict[str, str], requires_database: None
) -> None:
    response = client.post(
        "/admin/conversations/99000001/reply",
        headers=admin_headers,
        json={"text": "Hello?"},
    )
    assert response.status_code == 404
