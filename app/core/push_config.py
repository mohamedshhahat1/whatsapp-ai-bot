"""Configuration for mobile push notifications.

Why this is not in ``app/config.py``: it should be, and in a normal edit it
would be. These fields are separated for a mechanical reason -- ``config.py``
is a single 27 KB module, the tooling available to me replaces files whole
rather than patching them, and a previous whole-file rewrite of that module
silently dropped a hundred lines of explanatory comments. A second
``BaseSettings`` that reuses the same secret sources is the cheaper mistake.

Everything else follows the house rules: nothing is hardcoded, the feature is
off until switched on, and the credential is a secret resolved the same way
every other secret is -- environment variable, ``FCM_CREDENTIALS_FILE``,
Vault, or a Docker secret in ``SECRETS_DIR``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from app.core.secrets import (
    FileEnvSecretsSource,
    VaultSettingsSource,
    resolve_secrets_dir,
)


class PushSettings(BaseSettings):
    """Push notification configuration, resolved from the environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        secrets_dir=resolve_secrets_dir(),
    )

    # Off by default, and that default is load-bearing. Enabled with no
    # credentials, every sales lead would attempt a Google OAuth exchange,
    # fail, and write a warning -- turning a missing feature into a stream of
    # errors that trains people to ignore the logs.
    push_enabled: bool = False

    # From the Firebase console: Project settings -> General -> Project ID.
    # Not the project *number*, which is a different identifier and produces a
    # 404 from the messages:send endpoint.
    fcm_project_id: str = ""

    # The service-account JSON, verbatim. A secret: it is a private key that
    # can send notifications to every device in the project. Prefer
    # FCM_CREDENTIALS_FILE or a Docker secret over putting this in an
    # environment variable, where it lands in /proc and in every child
    # process.
    fcm_credentials: str = ""

    # Notification copy. Configurable because it is customer-facing product
    # text in a product whose operators do not read English, and because the
    # spec that asked for these strings will not be the last word on them.
    push_default_title: str = "New Customer Message"
    push_default_body: str = "You have a new customer message."

    # Android needs a notification channel id that the app has registered, or
    # a background notification is delivered and then dropped silently on
    # Android 8+. Must match the channel the Flutter app creates.
    push_android_channel: str = "whatsapp_ai_bot_alerts"

    push_timeout_seconds: float = Field(default=10.0, gt=0)
    # Bounded retries with jitter. A phone that is off does NOT need retrying
    # -- FCM already queues for offline devices, which is the one reliability
    # requirement that is somebody else's job.
    push_max_attempts: int = Field(default=3, ge=1)
    push_backoff_max_seconds: float = Field(default=8.0, gt=0)

    @property
    def configured(self) -> bool:
        """True when a send could actually be attempted.

        Checked instead of ``push_enabled`` alone so that switching the
        feature on without credentials fails once, loudly, at the first send
        rather than producing an authentication error per notification.
        """
        return bool(
            self.push_enabled and self.fcm_project_id and self.fcm_credentials
        )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Same precedence order as the main Settings class.

        Explicit values, then the environment, then ``<FIELD>_FILE``, then
        Vault, then Docker secrets, then ``.env`` last.
        """
        return (
            init_settings,
            env_settings,
            FileEnvSecretsSource(settings_cls),
            VaultSettingsSource(settings_cls),
            file_secret_settings,
            dotenv_settings,
        )


@lru_cache
def get_push_settings() -> PushSettings:
    """Cached push settings (one read per process)."""
    return PushSettings()
