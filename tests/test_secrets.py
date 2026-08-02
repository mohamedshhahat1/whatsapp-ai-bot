"""Precedence rules for the secret backends (see app/core/secrets.py)."""

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_file_env_variable_is_read(monkeypatch, tmp_path) -> None:
    secret_file = tmp_path / "openai_api_key"
    secret_file.write_text("sk-from-file\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY_FILE", str(secret_file))

    settings = Settings(_env_file=None)

    assert settings.openai_api_key == "sk-from-file"


def test_docker_secret_directory_is_read(monkeypatch, tmp_path) -> None:
    (tmp_path / "admin_api_key").write_text("super-secret\n", encoding="utf-8")
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)

    settings = Settings(_env_file=None, _secrets_dir=str(tmp_path))

    assert settings.admin_api_key == "super-secret"


def test_environment_variable_wins_over_docker_secret(monkeypatch, tmp_path) -> None:
    (tmp_path / "admin_api_key").write_text("from-file", encoding="utf-8")
    monkeypatch.setenv("ADMIN_API_KEY", "from-env")

    settings = Settings(_env_file=None, _secrets_dir=str(tmp_path))

    assert settings.admin_api_key == "from-env"


def test_production_rejects_placeholder_secrets(monkeypatch) -> None:
    for name in ("OPENAI_API_KEY", "OPENAI_API_KEY_FILE", "WHATSAPP_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, environment="production")

    assert "openai_api_key" in str(excinfo.value)


def test_production_accepts_fully_provided_secrets(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY_FILE", raising=False)
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)

    settings = Settings(
        _env_file=None,
        environment="production",
        openai_api_key="sk-real",
        whatsapp_token="EAAG-real",
        whatsapp_phone_number_id="123456789012345",
        whatsapp_verify_token="random-verify-token",
        whatsapp_app_secret="meta-app-secret",
        admin_api_key="strong-admin-key",
        redis_password="test-password",
    )

    assert settings.environment == "production"
