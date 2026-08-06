"""Mobile push notifications: who gets told, and what they may see.

The one place above ``app/integrations/fcm.py`` that knows notifications
exist. Callers say "a sales lead arrived on conversation 42"; everything else
-- the audience, the copy, the privacy rules, retiring dead tokens, the
metrics -- happens here.

What this module deliberately never receives, and therefore can never leak:
a phone number, a WhatsApp id, a message body, or a price. The only customer
datum it will accept at all is a display name, and only a device that has
explicitly opted into ``preview`` is shown even that.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.metrics import (
    PUSH_DELIVERY_LATENCY,
    PUSH_FAILED_TOTAL,
    PUSH_INVALID_TOKEN_TOTAL,
    PUSH_SENT_TOTAL,
    REGISTERED_DEVICES,
)
from app.core.push_config import PushSettings, get_push_settings
from app.integrations.fcm import (
    FcmClient,
    InvalidRegistrationToken,
    PushNotConfigured,
)
from app.models.device_token import (
    DISABLED_UNREGISTERED,
    DeviceToken,
)
from app.repositories.device_token import DeviceTokenRepository

logger = get_logger(__name__)

# The four events worth interrupting somebody for. A closed vocabulary rather
# than free text, because the mobile app routes on these strings and an
# unrecognised value would produce a notification the app cannot act on.
#
# Everything absent from this list is absent on purpose: an AI reply, a read
# receipt, a delivery receipt, a typing indicator and an internal system event
# are all things that happen constantly and that no operator needs to be woken
# for. A push system that fires on all of them gets muted within a day, which
# costs more than never having built it.
TYPE_SALES_LEAD = "sales_lead"
TYPE_HANDOFF = "handoff"
TYPE_ASSIGNED = "assigned"
TYPE_CUSTOMER_MESSAGE = "customer_message"

NOTIFICATION_TYPES = (
    TYPE_SALES_LEAD,
    TYPE_HANDOFF,
    TYPE_ASSIGNED,
    TYPE_CUSTOMER_MESSAGE,
)

# Titles per event. Bodies are NOT varied per event: the body is what appears
# on a locked screen, and the whole point of the privacy default is that it
# says the same uninformative thing every time.
_TITLES = {
    TYPE_SALES_LEAD: "New Sales Lead",
    TYPE_HANDOFF: "A Customer Needs a Person",
    TYPE_ASSIGNED: "Conversation Assigned to You",
    TYPE_CUSTOMER_MESSAGE: "New Customer Message",
}


class NotificationService:
    """Fans one notable event out to every registered device.

    Takes its session and its client by injection so that a test can hand it a
    fake FCM client and a real transaction, and so that the Celery worker and
    the API can each supply their own session.
    """

    def __init__(
        self,
        session: AsyncSession,
        client: FcmClient | None = None,
        settings: PushSettings | None = None,
    ) -> None:
        self.session = session
        self._settings = settings or get_push_settings()
        # Constructed lazily rather than in __init__: a deployment with push
        # switched off should not build an HTTP client per request.
        self._client = client
        self._devices = DeviceTokenRepository(session)

    def _fcm(self) -> FcmClient:
        if self._client is None:
            self._client = FcmClient(self._settings)
        return self._client

    def _body_for(self, device: DeviceToken, customer_name: str | None) -> str:
        """What this specific device is allowed to show on a locked screen.

        Per device, not per event, because privacy is a property of the phone.
        Two operators looking at the same sales lead can have made different
        choices, and building one body for the whole fan-out would hand a
        customer's name to a device that asked not to receive one.

        ``preview`` upgrades to the customer's NAME and nothing else -- never
        the message, never the number. An empty or missing name falls back to
        the private wording rather than rendering "New message from" with a
        blank after it.
        """
        if device.wants_preview and customer_name:
            return customer_name
        return self._settings.push_default_body

    async def notify(
        self,
        *,
        conversation_id: int,
        notification_type: str,
        customer_name: str | None = None,
    ) -> int:
        """Tell every registered device about one event; return how many took it.

        Never raises. A push failure must not be able to fail the work that
        triggered it -- by the time this runs the customer's message is
        committed and their reply may already have been sent, and an exception
        here would roll back or retry work that has visibly happened. This is
        the same trade ``app/core/events.py`` makes, for the same reason.

        Returns 0 when push is disabled or unconfigured, which is the normal
        state of a deployment that has not set up Firebase.
        """
        if notification_type not in NOTIFICATION_TYPES:
            # A programming error, not a runtime condition: fail loudly here
            # rather than sending a notification the app cannot route.
            raise ValueError(f"Unknown notification type: {notification_type!r}")

        if not self._settings.configured:
            return 0

        devices = await self._devices.enabled_devices()
        REGISTERED_DEVICES.set(len(devices))
        if not devices:
            # Worth a log line: an event that nobody could be told about looks
            # exactly like a working system from the outside.
            logger.info(
                "push_no_registered_devices",
                conversation_id=conversation_id,
                type=notification_type,
            )
            return 0

        # Only ever these three keys, per the payload rules. The id is
        # stringified because FCM rejects a data map containing a number.
        data = {
            "conversation_id": str(conversation_id),
            "notification_type": notification_type,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        title = _TITLES.get(notification_type, self._settings.push_default_title)

        accepted = 0
        retired: list[DeviceToken] = []
        for device in devices:
            if await self._send_one(
                device=device,
                title=title,
                body=self._body_for(device, customer_name),
                data=data,
            ):
                accepted += 1
            elif not device.enabled:
                retired.append(device)

        if retired:
            # Committed here rather than left to the caller. Retiring a dead
            # token is not part of the caller's unit of work, and a caller that
            # rolled back for its own reasons would resurrect tokens Firebase
            # has already told us are gone -- which then fail again on every
            # future event, forever.
            await self.session.commit()
            REGISTERED_DEVICES.set(await self._devices.count_enabled())

        logger.info(
            "push_fanout_completed",
            conversation_id=conversation_id,
            type=notification_type,
            devices=len(devices),
            accepted=accepted,
            retired=len(retired),
        )
        return accepted

    async def _send_one(
        self,
        *,
        device: DeviceToken,
        title: str,
        body: str,
        data: dict[str, str],
    ) -> bool:
        """Deliver to a single device, classifying whatever goes wrong.

        Three outcomes, and they are handled differently on purpose:

        * accepted -- Firebase took the message.
        * dead token -- retire the row so it is never tried again. This is the
          only path that mutates state.
        * anything else -- already retried by the client's backoff policy;
          counted and dropped.

        No customer data is logged on any path. The device id and platform are
        ours; the token is not logged either, since it identifies a phone.
        """
        started = time.monotonic()
        try:
            await self._fcm().send(
                token=device.token, title=title, body=body, data=data
            )
        except InvalidRegistrationToken as exc:
            await self._devices.disable(device.token, reason=DISABLED_UNREGISTERED)
            device.enabled = False
            PUSH_INVALID_TOKEN_TOTAL.labels(platform=device.platform).inc()
            logger.info(
                "push_token_retired",
                device_id=device.id,
                platform=device.platform,
                fcm_status=exc.status,
            )
            return False
        except PushNotConfigured as exc:
            PUSH_FAILED_TOTAL.labels(
                platform=device.platform, reason="not_configured"
            ).inc()
            logger.error("push_not_configured", error=str(exc))
            return False
        except Exception as exc:
            # Transient failures have already exhausted the retry policy by
            # the time they surface here.
            PUSH_FAILED_TOTAL.labels(platform=device.platform, reason="transient").inc()
            logger.warning(
                "push_send_failed",
                device_id=device.id,
                platform=device.platform,
                error=str(exc),
            )
            return False
        finally:
            # Recorded for failures as well as successes: a latency series
            # that silently excludes the slow attempts is how a degrading
            # dependency stays invisible.
            PUSH_DELIVERY_LATENCY.observe(time.monotonic() - started)

        PUSH_SENT_TOTAL.labels(
            platform=device.platform, type=data["notification_type"]
        ).inc()
        return True

    async def refresh_device_gauge(self) -> int:
        """Republish the registered-device count.

        Called after registration and removal so the gauge is right without
        waiting for the next notification -- a fresh process that has sent
        nothing yet would otherwise report zero devices while several are
        registered.
        """
        count = await self._devices.count_enabled()
        REGISTERED_DEVICES.set(count)
        return count
