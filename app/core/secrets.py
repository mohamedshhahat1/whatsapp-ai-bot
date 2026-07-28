"""Secret loading for production deployments.

The application must never depend on a ``.env`` file in production. Secrets are
resolved from several backends, highest priority first:

1. Values passed explicitly to ``Settings(...)`` (tests).
2. Environment variables (GitHub Actions secrets, systemd, Kubernetes ``env``).
3. ``<FIELD>_FILE`` environment variables pointing at a file that holds the
   value (Kubernetes projected volumes, Vault Agent templates, CI runners).
4. HashiCorp Vault (KV v2), enabled with ``VAULT_ENABLED=true``.
5. Docker secrets mounted in ``SECRETS_DIR`` (default ``/run/secrets``).
6. The ``.env`` file -- development only; never read when
   ``ENVIRONMENT=production``.

See ``docs/SECRETS.md`` for operational guidance.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

DEFAULT_SECRETS_DIR = "/run/secrets"
_TRUTHY = {"1", "true", "yes", "on"}


class SecretLoadError(RuntimeError):
    """Raised when a configured secret backend cannot be read."""


def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def resolve_secrets_dir() -> str | None:
    """Return the Docker secrets directory if it exists on this host.

    Returning ``None`` keeps pydantic-settings quiet on developer machines,
    where ``/run/secrets`` does not exist.
    """
    candidate = Path(os.environ.get("SECRETS_DIR", DEFAULT_SECRETS_DIR))
    return str(candidate) if candidate.is_dir() else None


def read_secret_file(path: str | os.PathLike[str]) -> str:
    """Read a secret from a file, stripping the trailing newline."""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SecretLoadError(f"Cannot read secret file {path!s}: {exc}") from exc


class FileEnvSecretsSource(PydanticBaseSettingsSource):
    """Resolve settings from ``<FIELD>_FILE`` environment variables.

    This is the convention used by Kubernetes projected volumes, Vault Agent
    templates and CI runners that write a secret to disk instead of exporting
    it into the process environment (where it would leak into ``/proc`` and
    child processes).

    Example: ``OPENAI_API_KEY_FILE=/run/secrets/openai_api_key``.
    """

    def get_field_value(
        self, field: FieldInfo, field_name: str
    ) -> tuple[Any, str, bool]:
        path = os.environ.get(f"{field_name.upper()}_FILE")
        if not path:
            return None, field_name, False
        return read_secret_file(path), field_name, False

    def __call__(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for field_name, field in self.settings_cls.model_fields.items():
            value, key, _ = self.get_field_value(field, field_name)
            if value is not None:
                values[key] = value
        return values


class VaultSettingsSource(PydanticBaseSettingsSource):
    """Resolve settings from a HashiCorp Vault KV v2 mount.

    Enabled with ``VAULT_ENABLED=true``. Authentication uses either a token
    (``VAULT_TOKEN`` or ``VAULT_TOKEN_FILE``) or AppRole (``VAULT_ROLE_ID`` +
    ``VAULT_SECRET_ID``). Keys stored in Vault map to settings fields
    case-insensitively, e.g. ``OPENAI_API_KEY`` -> ``openai_api_key``.

    Failures are raised, never swallowed: a deployment that expects Vault must
    not silently fall back to defaults.
    """

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._secrets: dict[str, Any] | None = None

    @property
    def enabled(self) -> bool:
        return _is_truthy(os.environ.get("VAULT_ENABLED"))

    def _authenticate(self, client: httpx.Client, addr: str) -> str:
        token = os.environ.get("VAULT_TOKEN", "").strip()
        token_file = os.environ.get("VAULT_TOKEN_FILE", "").strip()
        if not token and token_file:
            token = read_secret_file(token_file)
        if token:
            return token

        role_id = os.environ.get("VAULT_ROLE_ID", "").strip()
        secret_id = os.environ.get("VAULT_SECRET_ID", "").strip()
        if not (role_id and secret_id):
            raise SecretLoadError(
                "VAULT_ENABLED=true but no VAULT_TOKEN/VAULT_TOKEN_FILE or "
                "AppRole credentials (VAULT_ROLE_ID + VAULT_SECRET_ID) were given"
            )
        response = client.post(
            f"{addr}/v1/auth/approle/login",
            json={"role_id": role_id, "secret_id": secret_id},
        )
        response.raise_for_status()
        return str(response.json()["auth"]["client_token"])

    def _load(self) -> dict[str, Any]:
        addr = os.environ.get("VAULT_ADDR", "").strip().rstrip("/")
        path = os.environ.get("VAULT_SECRET_PATH", "").strip().strip("/")
        mount = os.environ.get("VAULT_KV_MOUNT", "secret").strip().strip("/")
        timeout = float(os.environ.get("VAULT_TIMEOUT", "5"))
        if not addr or not path:
            raise SecretLoadError(
                "VAULT_ENABLED=true requires VAULT_ADDR and VAULT_SECRET_PATH"
            )
        try:
            with httpx.Client(timeout=timeout) as client:
                token = self._authenticate(client, addr)
                response = client.get(
                    f"{addr}/v1/{mount}/data/{path}",
                    headers={"X-Vault-Token": token},
                )
                response.raise_for_status()
                payload = response.json()["data"]["data"]
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise SecretLoadError(
                f"Failed to read secrets from Vault at {addr}: {exc}"
            ) from exc
        return {str(key).lower(): value for key, value in payload.items()}

    def _cached(self) -> dict[str, Any]:
        if self._secrets is None:
            self._secrets = self._load()
        return self._secrets

    def get_field_value(
        self, field: FieldInfo, field_name: str
    ) -> tuple[Any, str, bool]:
        if not self.enabled:
            return None, field_name, False
        return self._cached().get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        if not self.enabled:
            return {}
        secrets = self._cached()
        return {
            name: secrets[name]
            for name in self.settings_cls.model_fields
            if name in secrets
        }
