"""How long audit history is kept.

Separate from :mod:`app.config` for the same reason ``inbound_config`` and
``push_config`` are: the main settings module can only be rewritten whole by
the tooling that edits it, and a previous whole-file rewrite silently dropped
a hundred lines of comments. Every one of these classes reads the environment
identically, so nothing about operating them differs.

Why audit logs expire at all
----------------------------
They are append-only by trigger (migration 0010) and one row is written per
state-changing admin action, so the table only ever grows. That is correct
and it is also unbounded: nothing in the schema distinguishes a log that is
evidence from one that is a delete-conversation click from four years ago.

Retention is the part of an audit policy that says how long "forever" is.
Keeping records beyond the period anyone would consult them is not extra
safety -- it is a growing table of personal data with no stated purpose,
which most data-protection regimes treat as a liability rather than a
precaution.

Why the default cannot surprise anyone
--------------------------------------
Three hundred and sixty-five days, and ``0`` disables expiry entirely. The
default deletes nothing for a year after this ships, because audit_logs was
created three migrations ago and cannot contain a row older than that. The
safety of the default is a property of the calendar, not a promise.
"""

from datetime import timedelta
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class RetentionSettings(BaseSettings):
    """Retention policy for the audit log."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    #: How many days of audit history to keep. Zero keeps everything, which
    #: is the behaviour every deployment had before this setting existed, so
    #: it is available to anyone whose compliance answer is "never delete".
    audit_retention_days: int = Field(default=365, ge=0)

    @property
    def audit_retention(self) -> timedelta:
        return timedelta(days=self.audit_retention_days)

    @property
    def enforced(self) -> bool:
        """Whether anything is ever deleted."""
        return self.audit_retention_days > 0


@lru_cache
def get_retention_settings() -> RetentionSettings:
    return RetentionSettings()
