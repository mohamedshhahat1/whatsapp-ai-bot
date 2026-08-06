"""User data access."""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.exceptions import ConflictError
from app.models.user import User
from app.repositories.base import BaseRepository

# Added by 0009_channel_identity. Referenced by name rather than inferred
# from its columns: ``external_id`` is nullable, and column inference is the
# kind of thing that silently resolves to the wrong index later.
_CHANNEL_IDENTITY_CONSTRAINT = "uq_users_channel_external_id"


class UserRepository(BaseRepository):
    async def get_by_wa_id(self, wa_id: str) -> User | None:
        return await self.session.scalar(select(User).where(User.wa_id == wa_id))

    async def get_or_create(self, wa_id: str, name: str | None = None) -> User:
        """Resolve a customer by wa_id, creating the row atomically.

        A customer who sends two messages in quick succession produces two
        webhook deliveries that two Celery workers can process concurrently.
        A plain SELECT-then-INSERT loses that race and one worker dies on the
        ``ix_users_wa_id`` unique violation, which then burns Celery retries.
        ``ON CONFLICT DO NOTHING`` makes the insert a no-op for the loser,
        which then re-reads the winner's row.
        """
        user = await self.get_by_wa_id(wa_id)
        if user is None:
            await self.session.execute(
                pg_insert(User)
                .values(wa_id=wa_id, name=name)
                .on_conflict_do_nothing(index_elements=[User.wa_id])
            )
            user = await self.get_by_wa_id(wa_id)
            if user is None:  # pragma: no cover - only if the winner rolled back
                raise ConflictError(f"Could not create or load customer {wa_id}")
        if name and user.name != name:
            user.name = name
        return user

    async def get_by_channel_id(self, channel: str, external_id: str) -> User | None:
        """Resolve a customer by their id on one specific channel.

        The same human on WhatsApp and on Messenger is two rows. Meta exposes
        no way to match a page-scoped id to a phone number, so there is no
        honest way to merge them and pretending otherwise would attach one
        customer's history to another.
        """
        return await self.session.scalar(
            select(User).where(
                User.channel == channel,
                User.external_id == external_id,
            )
        )

    async def get_or_create_by_channel(
        self, channel: str, external_id: str, name: str | None = None
    ) -> User:
        """Resolve a customer on a non-WhatsApp channel, creating atomically.

        Same race, same fix, different key. ``get_or_create`` keys its
        conflict on ``ix_users_wa_id``, which cannot work here: ``wa_id`` is
        NULL for every row this creates, and Postgres treats each NULL as
        distinct, so a unique index over it never fires. Two workers would
        both insert and the customer would end up with two identities.

        ``uq_users_channel_external_id`` is the constraint that describes
        identity on these channels, so that is what the conflict names.
        """
        user = await self.get_by_channel_id(channel, external_id)
        if user is None:
            await self.session.execute(
                pg_insert(User)
                .values(channel=channel, external_id=external_id, name=name)
                .on_conflict_do_nothing(constraint=_CHANNEL_IDENTITY_CONSTRAINT)
            )
            user = await self.get_by_channel_id(channel, external_id)
            if user is None:  # pragma: no cover - only if the winner rolled back
                raise ConflictError(
                    f"Could not create or load customer {external_id} on {channel}"
                )
        if name and user.name != name:
            user.name = name
        return user

    async def list(self, offset: int = 0, limit: int = 50) -> list[User]:
        result = await self.session.scalars(
            select(User).order_by(User.created_at.desc()).offset(offset).limit(limit)
        )
        return list(result)

    async def count(self) -> int:
        return int(await self.session.scalar(select(func.count(User.id))) or 0)
