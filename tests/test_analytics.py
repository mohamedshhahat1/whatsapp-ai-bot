"""Analytics regressions: wildcard search and time windows.

Two bugs are covered here. Searching for a term containing % returned every
message in the database, and the overview mixed a 30-day spend figure with a
lifetime conversation count, which made cost per conversation drift downwards
forever.
"""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.analytics import AnalyticsRepository, _escape_like
from app.repositories.message import MessageRepository
from tests.conftest import Customer


def test_escape_like_neutralises_wildcards() -> None:
    escaped = _escape_like("50% off_now")
    assert "\\%" in escaped
    assert "\\_" in escaped


def test_escape_like_escapes_the_escape_character_first() -> None:
    """Otherwise the backslash added for % would itself be escaped again."""
    assert _escape_like("a\\b") == "a\\\\b"


async def test_search_treats_percent_literally(
    db: AsyncSession, customer: Customer
) -> None:
    messages = MessageRepository(db)
    await messages.create(
        conversation_id=customer.conversation_id,
        direction="inbound",
        content=f"Is there a 50% discount {customer.wa_id}",
    )
    await messages.create(
        conversation_id=customer.conversation_id,
        direction="inbound",
        content=f"No wildcard here {customer.wa_id}",
    )
    await db.commit()

    analytics = AnalyticsRepository(db)

    # A bare wildcard must not behave like "match everything".
    for hit in await analytics.search_messages("%", limit=200):
        assert "%" in hit.content

    hits = await analytics.search_messages(f"50% discount {customer.wa_id}")
    assert len(hits) == 1
    assert "50%" in hits[0].content


async def test_underscore_is_not_a_single_character_wildcard(
    db: AsyncSession, customer: Customer
) -> None:
    await MessageRepository(db).create(
        conversation_id=customer.conversation_id,
        direction="inbound",
        content=f"axb {customer.wa_id}",
    )
    await db.commit()
    hits = await AnalyticsRepository(db).search_messages(f"a_b {customer.wa_id}")
    assert hits == []


async def test_activity_totals_separates_lifetime_from_window(
    db: AsyncSession, customer: Customer
) -> None:
    await MessageRepository(db).create(
        conversation_id=customer.conversation_id,
        direction="inbound",
        content="Recent message",
    )
    await db.commit()

    analytics = AnalyticsRepository(db)
    recent = await analytics.activity_totals(datetime.now(UTC) - timedelta(days=1))
    ancient = await analytics.activity_totals(datetime.now(UTC) + timedelta(days=1))

    assert recent.messages_in_period >= 1
    assert recent.active_conversations >= 1
    # A window that starts in the future contains nothing, while the lifetime
    # figures are unchanged. If these moved together the windows were mixed.
    assert ancient.messages_in_period == 0
    assert ancient.active_conversations == 0
    assert ancient.total_messages == recent.total_messages


def test_overview_exposes_every_field_the_dashboard_reads(
    client: TestClient, admin_headers: dict[str, str], requires_database: None
) -> None:
    """Contract test against dashboard/src/api.ts."""
    response = client.get("/admin/analytics/overview?days=30", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    for field in (
        "period_days",
        "since",
        "total_users",
        "total_conversations",
        "total_messages",
        "new_users",
        "new_conversations",
        "active_conversations",
        "messages_in_period",
        "ai_requests",
        "ai_errors",
        "error_rate",
        "avg_latency_ms",
        "p95_latency_ms",
        "cost",
        "cost_per_conversation_usd",
        "projected_monthly_cost_usd",
    ):
        assert field in body, f"overview is missing {field}"
