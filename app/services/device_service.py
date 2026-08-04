"""Registering and retiring the mobile devices that receive push.

Separate from ``NotificationService`` on purpose: enrolling a phone and
deciding who to interrupt are different responsibilities, and keeping them
apart means the notification path never needs a write method it does not use.

Both methods commit. Registration is the entire unit of work of its request --
there is nothing else in flight to coordinate with -- and leaving the commit to
the router would put transaction management back in the controller.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.metrics import REGISTERED_DEVICES
from app.models.device_token import DISABLED_BY_DEVICE, DeviceToken
from app.repositories.device_token import DeviceTokenRepository

logger = get_logger(__name__)


class DeviceService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.devices = DeviceTokenRepository(session)

    async def register(
        self, *, token: str, platform: str, notification_privacy: str
    ) -> DeviceToken:
        """Enrol or refresh one device.

        Idempotent: the app calls this on every launch and after every token
        rotation, and the repository upserts on the token, so a device that is
        already known is updated rather than duplicated.

        The token is never logged. It is the address of somebody's phone, and
        the device id is enough to follow a registration through the logs.
        """
        device = await self.devices.register(
            token=token,
            platform=platform,
            notification_privacy=notification_privacy,
        )
        await self.session.commit()
        REGISTERED_DEVICES.set(await self.devices.count_enabled())
        logger.info(
            "device_token_registered",
            device_id=device.id,
            platform=device.platform,
            privacy=device.notification_privacy,
        )
        return device

    async def disable(self, token: str) -> bool:
        """Stop notifying one device, at that device's own request.

        Returns False when the token was unknown or already off. The caller
        answers 204 either way: "stop sending to me" has been honoured in both
        cases, and distinguishing them would tell an unauthenticated-ish caller
        whether a given token is registered.
        """
        turned_off = await self.devices.disable(token, reason=DISABLED_BY_DEVICE)
        await self.session.commit()
        REGISTERED_DEVICES.set(await self.devices.count_enabled())
        if turned_off:
            logger.info("device_token_disabled", reason=DISABLED_BY_DEVICE)
        return turned_off
