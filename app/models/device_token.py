"""A mobile device registered to receive push notifications.

On the absence of an ``operator_id``: this system has no operator accounts.
Authentication is one shared ``ADMIN_API_KEY`` presented as ``X-API-Key``,
with no login endpoint and no session, so a request can be proven to come
from a trusted client but not from a particular person -- ``Conversation.
assigned_operator`` is free text a client supplies for the same reason.

A row here is therefore a DEVICE, not a person, and a notification fans out
to every enabled device. The consequence worth stating plainly: push cannot
be addressed to "the operator this conversation was assigned to", because
nothing in the database knows which device that is.

``notification_privacy`` sits on this row rather than on an operator record.
That is not purely a workaround -- the setting describes what may appear on
*this* phone's lock screen, and two people sharing one API key can
reasonably want different answers.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# The two platforms Firebase delivers to. Validated at the API boundary as
# well: an unrecognised value would be stored happily and then never match
# anything, which is a silent failure rather than a loud one.
PLATFORM_ANDROID = "android"
PLATFORM_IOS = "ios"
PLATFORMS = (PLATFORM_ANDROID, PLATFORM_IOS)

# How much a notification may reveal on a locked screen. ``private`` is the
# default deliberately: a phone on a desk is readable by whoever walks past,
# and the safe setting is the one you get without asking for it.
PRIVACY_PRIVATE = "private"
PRIVACY_PREVIEW = "preview"
PRIVACY_MODES = (PRIVACY_PRIVATE, PRIVACY_PREVIEW)

# Why a token stopped being usable. Kept rather than deleted so that a device
# that vanished can be told apart from one somebody signed out of, which is
# the difference between a bug and a Tuesday.
DISABLED_UNREGISTERED = "unregistered"  # Firebase says the token is gone
DISABLED_INVALID = "invalid"  # Firebase rejected its shape
DISABLED_BY_DEVICE = "by_device"  # the app asked us to stop


class DeviceToken(Base):
    __tablename__ = "device_tokens"

    __table_args__ = (
        # The token IS the identity of the row. Firebase issues one per app
        # install and reissues it on restore, reinstall or clear-data, so
        # registering must be idempotent on this column or a phone that is
        # merely opened twice would collect duplicate rows and be notified
        # twice per event.
        UniqueConstraint("token", name="uq_device_tokens_token"),
        # Every send starts by listing the enabled devices, and disabled rows
        # accumulate forever because they are never deleted.
        Index("ix_device_tokens_enabled", "enabled"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # 512 rather than 255: FCM tokens run to roughly 160 characters today but
    # Google has never documented a maximum, and a truncated token is an
    # undeliverable one that still looks valid in the table.
    token: Mapped[str] = mapped_column(String(512), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    notification_privacy: Mapped[str] = mapped_column(
        String(16),
        default=PRIVACY_PRIVATE,
        server_default=PRIVACY_PRIVATE,
        nullable=False,
    )
    # Soft delete. A token Firebase has rejected must stop being used
    # immediately, but deleting the row would lose the fact that it ever
    # existed -- and re-registering the same device would then look like a
    # brand new install rather than the reconnection it is.
    #
    # server_default as well as default because registration inserts through
    # pg_insert, which does not apply ORM-side defaults.
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    disabled_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Refreshed every time the app re-registers, which it does on every launch
    # and on every token rotation. A device that has not been seen for months
    # is almost certainly uninstalled; this is how that becomes visible
    # without waiting for Firebase to say so.
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    @property
    def wants_preview(self) -> bool:
        """True when this device has opted in to seeing more than the default.

        A property rather than a comparison at the call site so that the
        default-deny rule lives in one place: an unrecognised value read from
        an older row falls through to private, which is the safe answer.
        """
        return self.notification_privacy == PRIVACY_PREVIEW
