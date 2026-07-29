"""Regression: the closed 24-hour window is a 409, not a 500.

Meta rejects free-form messages more than 24 hours after the customer's last
message. The operator's request is valid, so returning 500 both misled the
operator and polluted the error rate that alerting watches.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.repositories.message import MessageRepository
from app.services.reply_service import OutsideServiceWindowError
from tests.conftest import Customer


def test_outside_window_error_is_a_conflict() -> None:
    assert issubclass(OutsideServiceWindowError, ConflictError)
    assert OutsideServiceWindowError.status_code == 409


async def test_reply_to_silent_customer_is_rejected(
    client: TestClient,
    admin_headers: dict[str, str],
    customer: Customer,
) -> None:
    """A customer who has never written in cannot be messaged free-form."""
    response = client.post(
        f"/admin/conversations/{customer.conversation_id}/reply",
        headers=admin_headers,
        json={"text": "Hello?"},
    )
    assert response.status_code == 409


async def test_reply_after_the_window_closes_is_rejected(
    db: AsyncSession,
    client: TestClient,
    admin_headers: dict[str, str],
    customer: Customer,
) -> None:
    message = await MessageRepository(db).create(
        conversation_id=customer.conversation_id,
        direction="inbound",
        content="Are you open on Sunday?",
        wa_message_id=f"wamid.{customer.wa_id}",
    )
    # Backdate past the service window.
    await db.execute(
        text("UPDATE messages SET created_at = now() - interval '30 hours' "
             "WHERE id = :id"),
        {"id": message.id},
    )
    await db.commit()

    response = client.post(
        f"/admin/conversations/{customer.conversation_id}/reply",
        headers=admin_headers,
        json={"text": "Sorry for the delay."},
    )
    assert response.status_code == 409
    assert "template" in response.text.lower()


@pytest.mark.parametrize("missing_id", [99_000_001])
async def test_reply_to_unknown_conversation_is_404(
    client: TestClient, admin_headers: dict[str, str], missing_id: int
) -> None:
    response = client.post(
        f"/admin/conversations/{missing_id}/reply",
        headers=admin_headers,
        json={"text": "Hello?"},
    )
    assert response.status_code == 404
