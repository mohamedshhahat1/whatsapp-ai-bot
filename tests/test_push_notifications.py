"""Mobile push notifications: registration, fan-out, retirement, privacy.

These run against the real database, like the other repository tests. The
upsert under test IS the ON CONFLICT clause and the unique constraint, and a
faked session would assert nothing about either.

Firebase itself is faked. There is no emulator for FCM, and a test that made
real calls would need a service-account key in CI -- the one credential this
feature must never let near a repository.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, get_args
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import conversation_activity, conversation_handoff
from app.core.push_config import PushSettings
from app.integrations.fcm import InvalidRegistrationToken
from app.models.conversation import MODE_BOT, MODE_HUMAN, TAG_SALES_LEAD
from app.models.device_token import (
    DISABLED_UNREGISTERED,
    PLATFORM_ANDROID,
    PLATFORM_IOS,
    PLATFORMS,
    PRIVACY_MODES,
    PRIVACY_PREVIEW,
    PRIVACY_PRIVATE,
    DeviceToken,
)
from app.repositories.device_token import DeviceTokenRepository
from app.schemas.device_token import Platform, PrivacyMode
from app.services.notification_service import (
    TYPE_ASSIGNED,
    TYPE_CUSTOMER_MESSAGE,
    TYPE_HANDOFF,
    TYPE_SALES_LEAD,
    NotificationService,
)
from app.services.push_dispatcher import _classify
from tests.conftest import run_db

CUSTOMER_NAME = "Ahmed Test"


def new_token() -> str:
    """A unique token, long enough to pass the schema's length floor."""
    return "pushtest-" + uuid4().hex


def push_settings() -> PushSettings:
    """Settings that look configured, with no real credentials.

    Constructor values outrank the environment, so this does not depend on
    whether the machine running the tests has FCM_* variables set.
    """
    return PushSettings(
        push_enabled=True,
        fcm_project_id="test-project",
        fcm_credentials="{}",
    )


@dataclass
class FakeFcm:
    """Stands in for FcmClient, recording what it was asked to deliver.

    ``dead`` tokens raise the permanent-failure exception. ``failing`` tokens
    raise a generic error, standing in for a transient failure that has
    already exhausted its retries by the time the service sees it.
    """

    sent: list[dict[str, Any]] = field(default_factory=list)
    dead: set[str] = field(default_factory=set)
    failing: set[str] = field(default_factory=set)

    async def send(
        self, *, token: str, title: str, body: str, data: dict[str, str]
    ) -> str:
        if token in self.dead:
            raise InvalidRegistrationToken("UNREGISTERED")
        if token in self.failing:
            raise RuntimeError("firebase unavailable")
        self.sent.append({"token": token, "title": title, "body": body, "data": data})
        return "projects/test-project/messages/1"

    def bodies_for(self, token: str) -> list[str]:
        return [item["body"] for item in self.sent if item["token"] == token]

    @property
    def tokens(self) -> set[str]:
        return {item["token"] for item in self.sent}


async def purge_tokens(session: AsyncSession, *tokens: str) -> None:
    """Remove the device rows a test created."""
    await session.execute(delete(DeviceToken).where(DeviceToken.token.in_(tokens)))
    await session.commit()


async def register(
    session: AsyncSession,
    token: str,
    *,
    platform: str = PLATFORM_ANDROID,
    privacy: str = PRIVACY_PRIVATE,
) -> DeviceToken:
    device = await DeviceTokenRepository(session).register(
        token=token, platform=platform, notification_privacy=privacy
    )
    await session.commit()
    return device


def service(session: AsyncSession, client: FakeFcm) -> NotificationService:
    return NotificationService(session, client, push_settings())  # type: ignore[arg-type]


# --- Registration ------------------------------------------------------------


async def test_registration_stores_the_device(db: AsyncSession) -> None:
    token = new_token()
    try:
        device = await register(db, token, platform=PLATFORM_IOS)
        assert device.platform == PLATFORM_IOS
        assert device.enabled is True
        # The default must be the private one. A permissive default would be a
        # privacy decision nobody made.
        assert device.notification_privacy == PRIVACY_PRIVATE
        assert device.last_seen_at is not None
    finally:
        await purge_tokens(db, token)


async def test_duplicate_token_updates_instead_of_duplicating(
    db: AsyncSession,
) -> None:
    """Re-registering the same token must update one row, not add a second."""
    token = new_token()
    try:
        first = await register(db, token, platform=PLATFORM_ANDROID)
        seen_at = first.last_seen_at
        second = await register(
            db, token, platform=PLATFORM_IOS, privacy=PRIVACY_PREVIEW
        )

        assert second.id == first.id
        assert second.platform == PLATFORM_IOS
        assert second.notification_privacy == PRIVACY_PREVIEW
        assert second.last_seen_at >= seen_at

        rows = await DeviceTokenRepository(db).enabled_devices()
        assert [row.token for row in rows].count(token) == 1
    finally:
        await purge_tokens(db, token)


async def test_registering_revives_a_retired_token(db: AsyncSession) -> None:
    """A device that comes back must start receiving notifications again.

    Otherwise a reinstall, or a phone that was off long enough for Firebase to
    drop its token, would go silent permanently.
    """
    token = new_token()
    try:
        await register(db, token)
        repository = DeviceTokenRepository(db)
        assert await repository.disable(token, reason=DISABLED_UNREGISTERED) is True
        await db.commit()

        revived = await register(db, token)
        assert revived.enabled is True
        assert revived.disabled_reason is None
    finally:
        await purge_tokens(db, token)


# --- Sending -----------------------------------------------------------------


async def test_notification_reaches_every_enabled_device(db: AsyncSession) -> None:
    """One event, several devices: the spec's multiple-devices case."""
    android, ios = new_token(), new_token()
    try:
        await register(db, android, platform=PLATFORM_ANDROID)
        await register(db, ios, platform=PLATFORM_IOS)
        client = FakeFcm()

        accepted = await service(db, client).notify(
            conversation_id=1234, notification_type=TYPE_SALES_LEAD
        )

        assert {android, ios} <= client.tokens
        assert accepted >= 2
    finally:
        await purge_tokens(db, android, ios)


async def test_disabled_device_is_not_sent_to(db: AsyncSession) -> None:
    token = new_token()
    try:
        await register(db, token)
        await DeviceTokenRepository(db).disable(token, reason=DISABLED_UNREGISTERED)
        await db.commit()
        client = FakeFcm()

        await service(db, client).notify(
            conversation_id=1234, notification_type=TYPE_HANDOFF
        )

        assert token not in client.tokens
    finally:
        await purge_tokens(db, token)


async def test_payload_carries_only_the_three_allowed_keys(
    db: AsyncSession,
) -> None:
    """No phone number, no wa_id, no message text, no prices."""
    token = new_token()
    try:
        await register(db, token)
        client = FakeFcm()

        await service(db, client).notify(
            conversation_id=4242,
            notification_type=TYPE_CUSTOMER_MESSAGE,
            customer_name=CUSTOMER_NAME,
        )

        payload = next(item for item in client.sent if item["token"] == token)
        assert set(payload["data"]) == {
            "conversation_id",
            "notification_type",
            "timestamp",
        }
        assert payload["data"]["conversation_id"] == "4242"
        assert payload["data"]["notification_type"] == TYPE_CUSTOMER_MESSAGE
    finally:
        await purge_tokens(db, token)


async def test_unknown_notification_type_is_rejected(db: AsyncSession) -> None:
    with pytest.raises(ValueError):
        await service(db, FakeFcm()).notify(
            conversation_id=1, notification_type="typing_indicator"
        )


# --- Invalid token removal ---------------------------------------------------


async def test_invalid_token_is_retired_automatically(db: AsyncSession) -> None:
    """A token Firebase rejects must never be tried again."""
    dead, alive = new_token(), new_token()
    try:
        await register(db, dead)
        await register(db, alive)
        client = FakeFcm(dead={dead})

        accepted = await service(db, client).notify(
            conversation_id=99, notification_type=TYPE_ASSIGNED
        )

        assert alive in client.tokens
        assert accepted >= 1

        repository = DeviceTokenRepository(db)
        retired = await repository.get_by_token(dead)
        assert retired is not None
        assert retired.enabled is False
        assert retired.disabled_reason == DISABLED_UNREGISTERED
        # Still present, not deleted: a retired device is history, not noise.
        assert dead not in {row.token for row in await repository.enabled_devices()}
    finally:
        await purge_tokens(db, dead, alive)


async def test_transient_failure_does_not_retire_the_token(
    db: AsyncSession,
) -> None:
    """Firebase being unavailable is not the device's fault."""
    token = new_token()
    try:
        await register(db, token)
        client = FakeFcm(failing={token})

        accepted = await service(db, client).notify(
            conversation_id=7, notification_type=TYPE_HANDOFF
        )

        assert accepted == 0
        survivor = await DeviceTokenRepository(db).get_by_token(token)
        assert survivor is not None
        assert survivor.enabled is True
    finally:
        await purge_tokens(db, token)


# --- Privacy -----------------------------------------------------------------


async def test_private_device_never_sees_the_customer_name(
    db: AsyncSession,
) -> None:
    token = new_token()
    try:
        await register(db, token, privacy=PRIVACY_PRIVATE)
        client = FakeFcm()

        await service(db, client).notify(
            conversation_id=1,
            notification_type=TYPE_CUSTOMER_MESSAGE,
            customer_name=CUSTOMER_NAME,
        )

        bodies = client.bodies_for(token)
        assert bodies == [push_settings().push_default_body]
        assert CUSTOMER_NAME not in bodies[0]
    finally:
        await purge_tokens(db, token)


async def test_preview_device_sees_the_name_and_nothing_else(
    db: AsyncSession,
) -> None:
    token = new_token()
    try:
        await register(db, token, privacy=PRIVACY_PREVIEW)
        client = FakeFcm()

        await service(db, client).notify(
            conversation_id=1,
            notification_type=TYPE_CUSTOMER_MESSAGE,
            customer_name=CUSTOMER_NAME,
        )

        assert client.bodies_for(token) == [CUSTOMER_NAME]
    finally:
        await purge_tokens(db, token)


async def test_two_devices_get_different_bodies_for_one_event(
    db: AsyncSession,
) -> None:
    """Privacy is per device, so one event produces two different payloads.

    This is the case that a shared, built-once body would silently break by
    leaking a name to the device that asked not to see one.
    """
    private_token, preview_token = new_token(), new_token()
    try:
        await register(db, private_token, privacy=PRIVACY_PRIVATE)
        await register(db, preview_token, privacy=PRIVACY_PREVIEW)
        client = FakeFcm()

        await service(db, client).notify(
            conversation_id=1,
            notification_type=TYPE_SALES_LEAD,
            customer_name=CUSTOMER_NAME,
        )

        assert client.bodies_for(private_token) == [push_settings().push_default_body]
        assert client.bodies_for(preview_token) == [CUSTOMER_NAME]
    finally:
        await purge_tokens(db, private_token, preview_token)


async def test_preview_falls_back_when_the_name_is_missing(
    db: AsyncSession,
) -> None:
    """An unnamed customer must not produce an empty notification body."""
    token = new_token()
    try:
        await register(db, token, privacy=PRIVACY_PREVIEW)
        client = FakeFcm()

        await service(db, client).notify(
            conversation_id=1,
            notification_type=TYPE_CUSTOMER_MESSAGE,
            customer_name=None,
        )

        assert client.bodies_for(token) == [push_settings().push_default_body]
    finally:
        await purge_tokens(db, token)


# --- Which events notify at all ---------------------------------------------


async def test_sales_lead_handoff_notifies(db: AsyncSession) -> None:
    event = conversation_handoff(
        conversation_id=1,
        mode=MODE_HUMAN,
        assigned_operator=None,
        reason="price",
        tag=TAG_SALES_LEAD,
    )
    assert await _classify(db, event, 1) == TYPE_SALES_LEAD


async def test_assignment_notifies(db: AsyncSession) -> None:
    event = conversation_handoff(
        conversation_id=1,
        mode=MODE_HUMAN,
        assigned_operator="Sara",
        reason="takeover",
    )
    assert await _classify(db, event, 1) == TYPE_ASSIGNED


async def test_plain_handoff_notifies(db: AsyncSession) -> None:
    event = conversation_handoff(
        conversation_id=1,
        mode=MODE_HUMAN,
        assigned_operator=None,
        reason="customer_asked",
    )
    assert await _classify(db, event, 1) == TYPE_HANDOFF


async def test_ai_resuming_notifies_nobody(db: AsyncSession) -> None:
    """Handing a conversation back to the bot is not worth a notification."""
    event = conversation_handoff(
        conversation_id=1,
        mode=MODE_BOT,
        assigned_operator=None,
        reason="resume_ai",
    )
    assert await _classify(db, event, 1) is None


async def test_outbound_activity_notifies_nobody(db: AsyncSession) -> None:
    """An AI reply or an operator's own reply must never buzz a phone."""
    event = conversation_activity(conversation_id=1, inbound=False)
    assert await _classify(db, event, 1) is None


async def test_inbound_message_in_bot_mode_notifies_nobody(db: AsyncSession) -> None:
    """The AI answers these within seconds; nobody needs waking.

    Uses a conversation id that cannot exist, so the mode lookup finds nothing
    -- which must also mean silence rather than a notification.
    """
    event = conversation_activity(conversation_id=-1, inbound=True)
    assert await _classify(db, event, -1) is None


async def test_inbound_message_in_human_mode_notifies(db: AsyncSession) -> None:
    """The one case that needs a database read to decide."""
    from app.models.conversation import Conversation
    from tests.conftest import create_customer, new_wa_id, purge

    wa_id = new_wa_id()
    created = await create_customer(db, wa_id)
    try:
        conversation = await db.get(Conversation, created.conversation_id)
        assert conversation is not None
        conversation.mode = MODE_HUMAN
        await db.commit()

        event = conversation_activity(
            conversation_id=created.conversation_id, inbound=True
        )
        classified = await _classify(db, event, created.conversation_id)
        assert classified == TYPE_CUSTOMER_MESSAGE
    finally:
        await purge(db, wa_id)


# --- API surface -------------------------------------------------------------


def test_registration_requires_the_admin_key(client: Any) -> None:
    response = client.post(
        "/admin/device-token",
        json={"token": new_token(), "platform": PLATFORM_ANDROID},
    )
    assert response.status_code == 401


def test_unknown_platform_is_rejected(
    client: Any, admin_headers: dict[str, str]
) -> None:
    """Section 9: validate platform values."""
    response = client.post(
        "/admin/device-token",
        headers=admin_headers,
        json={"token": new_token(), "platform": "blackberry"},
    )
    assert response.status_code == 422


def test_short_token_is_rejected(client: Any, admin_headers: dict[str, str]) -> None:
    response = client.post(
        "/admin/device-token",
        headers=admin_headers,
        json={"token": "too-short", "platform": PLATFORM_ANDROID},
    )
    assert response.status_code == 422


def test_register_and_delete_through_the_api(
    client: Any, admin_headers: dict[str, str], requires_database: None
) -> None:
    token = new_token()
    try:
        created = client.post(
            "/admin/device-token",
            headers=admin_headers,
            json={
                "token": token,
                "platform": PLATFORM_ANDROID,
                "notification_privacy": PRIVACY_PREVIEW,
            },
        )
        assert created.status_code == 200
        body = created.json()
        assert body["platform"] == PLATFORM_ANDROID
        assert body["notification_privacy"] == PRIVACY_PREVIEW
        assert body["enabled"] is True
        # The token must not be echoed back.
        assert "token" not in body

        removed = client.request(
            "DELETE",
            "/admin/device-token",
            headers=admin_headers,
            json={"token": token},
        )
        assert removed.status_code == 204

        stored = run_db(
            lambda session: DeviceTokenRepository(session).get_by_token(token)
        )
        assert stored is not None
        assert stored.enabled is False
    finally:
        run_db(lambda session: purge_tokens(session, token))


def test_deleting_an_unknown_token_still_succeeds(
    client: Any, admin_headers: dict[str, str], requires_database: None
) -> None:
    """204, not 404: the endpoint must not reveal which tokens are registered."""
    response = client.request(
        "DELETE",
        "/admin/device-token",
        headers=admin_headers,
        json={"token": new_token()},
    )
    assert response.status_code == 204


# --- Guards ------------------------------------------------------------------


def test_schema_literals_match_the_model_constants() -> None:
    """The API's vocabulary and the model's must not drift apart.

    typing.Literal cannot reference the constants, so these are two lists that
    happen to agree. Nothing but this test keeps them agreeing.
    """
    assert set(get_args(Platform)) == set(PLATFORMS)
    assert set(get_args(PrivacyMode)) == set(PRIVACY_MODES)


def test_push_is_not_configured_by_default() -> None:
    """An unconfigured deployment must never attempt a send.

    If this ever passes with push enabled, every sales lead on a deployment
    without Firebase credentials starts logging an authentication failure.
    """
    settings = PushSettings(push_enabled=False)
    assert settings.configured is False
    assert PushSettings(push_enabled=True, fcm_project_id="").configured is False
