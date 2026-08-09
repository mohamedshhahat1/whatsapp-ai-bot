"""An end user of the bot, identified per channel and per tenant.

Identity is (tenant_id, channel, external_id). The same human writing from
WhatsApp and from Messenger is two rows, because Meta gives us no way to know
they are the same person: a PSID, an IGSID and a phone number share nothing
that can be matched on. One row with several ids would need a merge rule that
could only ever be a guess, and a wrong guess shows one customer another
customer's history.

The tenant joined that key in 0016_tenant_ownership. Two businesses on this
deployment may each legitimately count the same phone number as a customer,
and without the tenant in the key the second one's inbound message resolves to
the first one's row -- which is a merge of customer identity, not a read leak.

The useful side effect is that uq_active_conversation_per_user keeps working
unchanged: one active conversation per user row now means one per person per
channel per tenant, which is the rule we would have written anyway.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.channels.constants import WHATSAPP
from app.db.base import Base

if TYPE_CHECKING:
    from app.models.conversation import Conversation


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "channel",
            "external_id",
            name="uq_users_channel_external_id",
        ),
        # Scoped in the same migration step as the constraint above, never
        # alone. A global unique wa_id left in force while the pair became
        # tenant-scoped is worse than changing neither: the second tenant's
        # insert trips it, on_conflict_do_nothing swallows the conflict, and
        # the re-read hands back the FIRST tenant's customer with no error
        # raised anywhere.
        UniqueConstraint("tenant_id", "wa_id", name="uq_users_tenant_wa_id"),
        # The key conversations point at. A foreign key must reference a unique
        # constraint over exactly the columns it names, so the tenant-carrying
        # child key needs this; the primary key on id alone cannot serve it.
        UniqueConstraint("tenant_id", "id", name="uq_users_tenant_scoped_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # RESTRICT. A tenant that still has customers is suspended, never deleted
    # out from under its own history.
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), index=True
    )
    # Which app this person writes from. server_default as well as default,
    # because inserts that go through pg_insert never apply ORM-side defaults.
    channel: Mapped[str] = mapped_column(
        String(24), default=WHATSAPP, server_default=WHATSAPP, index=True
    )
    # The platform's own id for them: wa_id, PSID or IGSID. Unique only within
    # a tenant and a channel, which is why the constraint above spans all three.
    #
    # NOT NULL since 0013_external_id_not_null. It was nullable through the
    # expand phase of 0009_channel_identity, while the WhatsApp writer still
    # populated wa_id alone; that writer now fills both and every historical
    # row was backfilled, so this is the one identifier every row is
    # guaranteed to have.
    external_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # WhatsApp's phone-number id. Nullable, because a Messenger user has no
    # phone number and there is nothing honest to put here.
    #
    # The index stays for lookups and loses only its uniqueness, which moved
    # to uq_users_tenant_wa_id above. Postgres still permits many NULLs in
    # that constraint, so WhatsApp keeps its per-tenant guarantee while other
    # channels leave the column empty.
    wa_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    name: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    conversations: Mapped[list[Conversation]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
