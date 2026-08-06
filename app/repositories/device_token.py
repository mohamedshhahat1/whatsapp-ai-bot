"""Device token data access.

Follows the convention of every other repository here: methods flush, never
commit. The caller owns the transaction boundary.
"""

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.device_token import DeviceToken
from app.repositories.base import BaseRepository


class DeviceTokenRepository(BaseRepository):
    async def register(
        self,
        *,
        token: str,
        platform: str,
        notification_privacy: str,
    ) -> DeviceToken:
        """Record that this device is listening, creating the row if new.

        An upsert on the token rather than a read followed by a write, and
        that distinction is the whole point of the method. Firebase reissues a
        registration token on reinstall, restore-from-backup and clear-data,
        and the app re-registers on every single launch -- so this runs
        constantly with a token that usually already exists. A select-then-
        insert would race with itself the moment an operator opened the app on
        two devices at once, and the loser would violate the unique
        constraint mid-request.

        Re-registering deliberately re-enables the row and clears
        ``disabled_reason``. A token we had given up on has, by definition,
        just proved it is alive again: the app is running and Firebase handed
        it the same token. Leaving it disabled would mean a phone that came
        back from a long flight, or a reinstall, silently never received
        another notification.

        ``updated_at`` is set explicitly because ``onupdate`` is an ORM-side
        hook and does not fire for an ON CONFLICT DO UPDATE.
        """
        now = datetime.now(UTC)
        device_id = await self.session.scalar(
            pg_insert(DeviceToken)
            .values(
                token=token,
                platform=platform,
                notification_privacy=notification_privacy,
                enabled=True,
                disabled_reason=None,
                last_seen_at=now,
            )
            .on_conflict_do_update(
                constraint="uq_device_tokens_token",
                set_={
                    "platform": platform,
                    "notification_privacy": notification_privacy,
                    "enabled": True,
                    "disabled_reason": None,
                    "last_seen_at": now,
                    "updated_at": now,
                },
            )
            .returning(DeviceToken.id)
        )
        await self.session.flush()
        device = await self.session.get(DeviceToken, device_id)
        if device is not None:
            # The upsert wrote through SQL, so an instance already in the
            # identity map still holds the pre-update column values.
            await self.session.refresh(device)
            return device
        # Unreachable in practice: the id came from RETURNING in this
        # transaction.
        raise RuntimeError(  # pragma: no cover
            f"device token {device_id} vanished immediately after upsert"
        )

    async def disable(self, token: str, *, reason: str) -> bool:
        """Stop sending to one device, keeping the row.

        Returns True when a row was actually turned off, so a caller can tell
        "we just retired a live token" from "that token was already gone" --
        the metric for invalid tokens should count the former only.

        Idempotent by the ``enabled`` predicate: Firebase can reject the same
        dead token on several concurrent sends, and only the first of those is
        news.

        The cast names what SQLAlchemy already returns. ``execute`` is typed
        as ``Result[Any]``, which has no ``rowcount``; a DML statement gives
        back a ``CursorResult``, which does. It is a no-op at runtime.
        """
        result = cast(
            "CursorResult[Any]",
            await self.session.execute(
                update(DeviceToken)
                .where(DeviceToken.token == token, DeviceToken.enabled.is_(True))
                .values(enabled=False, disabled_reason=reason)
                .execution_options(synchronize_session=False)
            ),
        )
        return bool(result.rowcount)

    async def enabled_devices(self) -> list[DeviceToken]:
        """Every device currently expecting notifications.

        The audience for one event. Not filtered by operator, because nothing
        in this schema knows which device belongs to whom -- see the model's
        module docstring.
        """
        result = await self.session.scalars(
            select(DeviceToken)
            .where(DeviceToken.enabled.is_(True))
            .order_by(DeviceToken.id)
        )
        return list(result)

    async def get_by_token(self, token: str) -> DeviceToken | None:
        return await self.session.scalar(
            select(DeviceToken).where(DeviceToken.token == token)
        )

    async def count_enabled(self) -> int:
        """How many devices would receive the next notification.

        Exported as a gauge. A push system that quietly has zero registered
        devices looks identical to one that is working, right up until
        somebody asks why nobody was told about a lead.
        """
        return int(
            await self.session.scalar(
                select(func.count(DeviceToken.id)).where(DeviceToken.enabled.is_(True))
            )
            or 0
        )
