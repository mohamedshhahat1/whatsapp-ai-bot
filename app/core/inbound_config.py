"""How old an inbound webhook delivery may be and still be answered.

Separate from :mod:`app.config` for the same reason ``push_config`` is: the
main settings module can only be rewritten whole by the tooling that edits it,
and a previous whole-file rewrite silently dropped a hundred lines of
comments. Both classes read the environment identically, so nothing about
operating them differs.

Why a freshness policy exists at all
------------------------------------
Meta retries a webhook it could not deliver, on its own exponential backoff,
for up to seven days. Those retries are indistinguishable from live traffic
unless something checks the ``timestamp`` the payload carries -- and treating a
forty-minute-old message as a live one is how a closed session gets reopened
and greeted with nobody on the other end having done anything.

The default is ten minutes. Meta delivers in seconds when it is healthy, so
anything approaching this bound is a redelivery after an outage rather than a
slow network, and answering it is worse than staying quiet: the customer has
long since put their phone down, and a welcome arriving out of nowhere reads as
the bot talking to itself.

Kept comfortably BELOW ``CONVERSATION_REOPEN_WINDOW_MINUTES`` (default 30) on
purpose. Inside the reopen window a late delivery lands back in the session it
belongs to and is harmless; the dangerous zone is past it, where a new session
is minted and a welcome is owed. A max age below the window means the gate
closes before that zone is ever reached.
"""

from datetime import timedelta
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class InboundSettings(BaseSettings):
    """Freshness policy for inbound webhook deliveries."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    #: Master switch. Off, every delivery is answered however old it is --
    #: which is exactly the behaviour that produced the unprompted welcome, so
    #: it exists to disable the gate in an incident, not as a preference.
    reject_stale_inbound: bool = True

    #: How old a delivery may be and still be answered. Zero disables the
    #: gate, matching the master switch, so neither can be defeated by the
    #: other being set the other way.
    inbound_max_age_minutes: int = Field(default=10, ge=0)

    @property
    def inbound_max_age(self) -> timedelta:
        return timedelta(minutes=self.inbound_max_age_minutes)

    @property
    def enforced(self) -> bool:
        """Whether the gate actually runs."""
        return self.reject_stale_inbound and self.inbound_max_age_minutes > 0


@lru_cache
def get_inbound_settings() -> InboundSettings:
    return InboundSettings()
