"""Operator accounts and the sessions they authenticate with."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.passwords import UNUSABLE_PASSWORD_HASH
from app.db.base import Base

# The reserved account that shared-ADMIN_API_KEY requests are attributed to.
#
# The key predates operator accounts and the mobile client still authenticates
# with it and nothing else, so it cannot simply be withdrawn. Nor can it stay
# an anonymous bypass, because then "every admin action has an operator" would
# be false for exactly the requests that are hardest to account for. Pointing
# it at a reserved row keeps operator_id non-nullable everywhere and turns
# remaining legacy usage into something a query can find.
LEGACY_OPERATOR_USERNAME = "legacy-api-key"


class Operator(Base):
    """One person who can administer the bot."""

    __tablename__ = "operators"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    # Encoded by app.core.passwords. Never a plaintext password, and never
    # compared with ==; see verify_password.
    password_hash: Mapped[str] = mapped_column(String(255))
    # Deactivation rather than deletion is the way an operator leaves. Their
    # audit rows reference this row with ondelete RESTRICT, so a DELETE would
    # either fail or take the trail with it.
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true"), nullable=False
    )
    is_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    sessions: Mapped[list[OperatorSession]] = relationship(
        back_populates="operator", cascade="all, delete-orphan"
    )

    @property
    def can_log_in(self) -> bool:
        """True when this account may exchange a password for a session.

        False for the reserved legacy account, whose hash is a sentinel no
        password produces. That account exists to be attributed to, not to be
        signed into, and the distinction is enforced here rather than left to
        every caller to remember.
        """
        return self.is_active and self.password_hash != UNUSABLE_PASSWORD_HASH


class OperatorSession(Base):
    """A bearer session issued to an operator, valid until it expires."""

    __tablename__ = "operator_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    operator_id: Mapped[int] = mapped_column(
        ForeignKey("operators.id", ondelete="CASCADE"), index=True
    )
    # SHA-256 of the token, hex encoded -- never the token itself. A database
    # disclosure should not hand over live sessions, and nothing needs the
    # original: authentication hashes the presented token and looks it up.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # Set on explicit logout. Kept as a column rather than deleting the row so
    # that a session ending is itself a fact with a time on it.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # 45 characters is the longest possible textual IPv6 address, including an
    # IPv4-mapped suffix.
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    operator: Mapped[Operator] = relationship(back_populates="sessions")

    def is_valid(self, now: datetime | None = None) -> bool:
        """True while this session still authenticates its operator."""
        if self.revoked_at is not None:
            return False
        return self.expires_at > (now or datetime.now(UTC))
