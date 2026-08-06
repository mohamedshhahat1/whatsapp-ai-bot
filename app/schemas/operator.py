"""Operator authentication schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    # Bounded so that an oversized body is rejected by validation rather than
    # reaching the KDF -- scrypt costs ~16 MiB per call and an unbounded
    # password field is a cheap way to make somebody else pay for it.
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class OperatorRead(BaseModel):
    """An operator account as the API reports it.

    password_hash is deliberately absent, and this is the only shape the
    routers serialise an Operator through.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str
    is_active: bool
    is_admin: bool
    last_login_at: datetime | None = None


class LoginResponse(BaseModel):
    token: str
    # Returned so a client can renew before expiry rather than discovering it
    # through a 401 in the middle of somebody's work.
    expires_at: datetime
    operator: OperatorRead


class WhoAmIRead(BaseModel):
    """Who the current credential belongs to.

    ``via_legacy_key`` is true when the caller authenticated with the shared
    ADMIN_API_KEY rather than as an operator. It is surfaced rather than
    hidden so a dashboard can say so, and so the migration away from the
    shared key is measurable instead of assumed.
    """

    operator_id: int | None
    username: str
    is_admin: bool
    via_legacy_key: bool
