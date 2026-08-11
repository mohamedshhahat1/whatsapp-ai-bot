"""User data access."""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.channels.constants import WHATSAPP
from app.core.exceptions import ConflictError
from app.models.user import User
from app.repositories.base import BaseRepository
from app.repositories.tenant import resolve_tenant_id

# Added by 0009_channel_identity, tenant-scoped by 0016_tenant_ownership.
# Referenced by name rather than inferred from its columns: column inference
# is the kind of thing that silently resolves to the wrong index later.
_CHANNEL_IDENTITY_CONSTRAINT = "uq_users_channel_external_id"


class UserRepository(BaseRepository):
    async def get_by_wa_id(self, wa_id: str, *, tenant_id: int) -> User | None:
        """Resolve a customer by phone number, within one tenant.

        ``tenant_id`` is mandatory and keyword-only. This read was unscoped
        until Phase 1c, and 0016 is what made that dangerous rather than
        merely untidy: it replaced the global ``ix_users_wa_id`` with
        ``uq_users_tenant_wa_id``, so the same phone number may now
        legitimately exist in several tenants. An unscoped lookup returns an
        arbitrary one of them, and whichever row Postgres happened to hand
        back would decide whose customer was answered, out of whose history.

        There is deliberately no default. A default would be a tenant this
        method invented, and an invented tenant is how a lookup stops failing
        and starts quietly answering for the wrong company.

        This is also the lookup ``get_or_create`` uses. The private helper
        that used to sit beside it ran exactly this query, and keeping two
        spellings of one predicate is how the scoped and unscoped versions
        drifted apart in the first place.
        """
        return await self.session.scalar(
            select(User).where(User.wa_id == wa_id, User.tenant_id == tenant_id)
        )

    async def get_or_create(
        self, wa_id: str, name: str | None = None, *, tenant_id: int | None = None
    ) -> User:
        """Resolve a WhatsApp customer by wa_id, creating the row atomically.

        A customer who sends two messages in quick succession produces two
        webhook deliveries that two Celery workers can process concurrently.
        A plain SELECT-then-INSERT loses that race and one worker dies on a
        unique violation, which then burns Celery retries. ``ON CONFLICT DO
        NOTHING`` makes the insert a no-op for the loser, which then re-reads
        the winner's row.

        Since 0013_external_id_not_null the insert also fills ``channel`` and
        ``external_id``. ``external_id`` is the wa_id: on WhatsApp the phone
        number IS the platform's own id for the customer, so copying it is
        not a placeholder, it is the same fact recorded under the name every
        channel uses.

        Both the lookup and the re-read are scoped to the tenant since
        0016_tenant_ownership. That is the fix for the defect where a second
        tenant's inbound message resolved to the first tenant's customer and
        was answered out of the first tenant's history. ``tenant_id`` remains
        optional HERE, unlike on the read paths above, and that is a decision
        rather than an oversight: this is a write path reached by an inbound
        webhook, which carries no operator credential to resolve a tenant
        from. Under D-5 those deliveries resolve to the deployment's original
        tenant for now, and real per-integration resolution is Phase 3.

        The conflict target is deliberately left unnamed. This row trips two
        unique constraints when it duplicates an existing customer --
        ``uq_users_tenant_wa_id`` and ``uq_users_channel_external_id`` -- and
        Postgres reports whichever it reaches first. Naming one would leave
        the other free to raise IntegrityError, which is the exact failure
        this clause exists to prevent.
        """
        owner = await resolve_tenant_id(self.session, tenant_id)
        user = await self.get_by_wa_id(wa_id, tenant_id=owner)
        if user is None:
            await self.session.execute(
                pg_insert(User)
                .values(
                    tenant_id=owner,
                    channel=WHATSAPP,
                    external_id=wa_id,
                    wa_id=wa_id,
                    name=name,
                )
                .on_conflict_do_nothing()
            )
            user = await self.get_by_wa_id(wa_id, tenant_id=owner)
            if user is None:  # pragma: no cover - only if the winner rolled back
                raise ConflictError(f"Could not create or load customer {wa_id}")
        if name and user.name != name:
            user.name = name
        return user

    async def get_by_channel_id(
        self, channel: str, external_id: str, *, tenant_id: int
    ) -> User | None:
        """Resolve a customer by their id on one specific channel.

        The same human on WhatsApp and on Messenger is two rows. Meta exposes
        no way to match a page-scoped id to a phone number, so there is no
        honest way to merge them and pretending otherwise would attach one
        customer's history to another.

        Scoped on the same terms as :meth:`get_by_wa_id`; see the note there.
        Two tenants running Instagram DMs through the same agency can hold
        the same page-scoped id, so this needs the predicate for the same
        reason the phone-number lookup does.
        """
        return await self.session.scalar(
            select(User).where(
                User.channel == channel,
                User.external_id == external_id,
                User.tenant_id == tenant_id,
            )
        )

    async def get_or_create_by_channel(
        self,
        channel: str,
        external_id: str,
        name: str | None = None,
        *,
        tenant_id: int | None = None,
    ) -> User:
        """Resolve a customer on a non-WhatsApp channel, creating atomically.

        Same race, same fix, different key. ``get_or_create`` could not name
        this constraint before 0013 because ``external_id`` was NULL for every
        row it wrote, and Postgres treats each NULL as distinct, so a unique
        index over it never fired. Two workers would both insert and the
        customer would end up with two identities.

        ``uq_users_channel_external_id`` is the constraint that describes
        identity on these channels, so that is what the conflict names. It
        keeps that name after 0016 widened it to include the tenant,
        specifically so this clause did not have to change. It stays named
        here, unlike in ``get_or_create``: these rows have a NULL ``wa_id``,
        and Postgres allows many NULLs in a unique constraint, so this remains
        the only one they can violate.
        """
        owner = await resolve_tenant_id(self.session, tenant_id)
        user = await self.get_by_channel_id(channel, external_id, tenant_id=owner)
        if user is None:
            await self.session.execute(
                pg_insert(User)
                .values(
                    tenant_id=owner,
                    channel=channel,
                    external_id=external_id,
                    name=name,
                )
                .on_conflict_do_nothing(constraint=_CHANNEL_IDENTITY_CONSTRAINT)
            )
            user = await self.get_by_channel_id(channel, external_id, tenant_id=owner)
            if user is None:  # pragma: no cover - only if the winner rolled back
                raise ConflictError(
                    f"Could not create or load customer {external_id} on {channel}"
                )
        if name and user.name != name:
            user.name = name
        return user

    async def list(
        self, offset: int = 0, limit: int = 50, *, tenant_id: int
    ) -> list[User]:
        """This tenant's customers, newest first.

        A true enumeration, and therefore an authorization boundary rather
        than a tidiness question: unscoped, this handed every customer in the
        deployment -- names and phone numbers included -- to whichever
        operator asked for page one.
        """
        result = await self.session.scalars(
            select(User)
            .where(User.tenant_id == tenant_id)
            .order_by(User.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result)

    async def count(self, *, tenant_id: int) -> int:
        """How many customers this tenant has."""
        return int(
            await self.session.scalar(
                select(func.count(User.id)).where(User.tenant_id == tenant_id)
            )
            or 0
        )
