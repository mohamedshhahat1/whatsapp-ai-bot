"""Regression tests for Redis authentication.

These target ``apply_redis_credentials`` rather than a live Redis, because it
is the single point every consumer depends on: the app, the worker, the Celery
broker and result backend, the rate limiter, the quota counters and the
idempotency cache all derive their connection from ``redis_url``. If this
function is right, all of them authenticate; if it is wrong, all of them fail
the same way.
"""

import pytest

from app.config import Settings, apply_redis_credentials

BASE = "redis://redis:6379/0"


def test_password_is_injected() -> None:
    url = apply_redis_credentials(BASE, password="s3cret")
    assert url == "redis://:s3cret@redis:6379/0"


def test_database_number_is_preserved() -> None:
    """Losing the database index would silently mix queues and quota keys."""
    url = apply_redis_credentials("redis://redis:6379/3", password="pw")
    assert url.endswith("/3")


def test_password_is_percent_encoded() -> None:
    """The failure this prevents is subtle and expensive.

    ``openssl rand -base64`` emits ``/`` and ``+``. Unencoded, the ``/`` ends
    the authority section early: the client connects to database 0 with a
    truncated password rather than failing, so the symptom is wrong-database
    behaviour rather than an auth error.
    """
    url = apply_redis_credentials(BASE, password="a/b+c=d")
    assert "a/b+c=d" not in url
    assert "a%2Fb%2Bc%3Dd" in url
    assert url.endswith("/0")


def test_username_is_included_when_set() -> None:
    url = apply_redis_credentials(BASE, username="appuser", password="pw")
    assert url == "redis://appuser:pw@redis:6379/0"


def test_existing_credentials_win() -> None:
    """An operator who wrote credentials into REDIS_URL meant them."""
    explicit = "redis://:already@redis:6379/0"
    assert apply_redis_credentials(explicit, password="other") == explicit


def test_tls_switches_scheme() -> None:
    url = apply_redis_credentials(BASE, password="pw", tls=True)
    assert url.startswith("rediss://")


def test_tls_without_password_still_switches_scheme() -> None:
    url = apply_redis_credentials(BASE, tls=True)
    assert url == "rediss://redis:6379/0"


def test_no_password_is_a_no_op() -> None:
    """Backward compatibility: unconfigured deployments are untouched."""
    assert apply_redis_credentials(BASE) == BASE


def test_empty_url_is_untouched() -> None:
    """celery_broker_url defaults to empty and must stay that way.

    An empty broker URL is the signal to fall back to redis_url. Returning
    something non-empty here would break that fallback.
    """
    assert apply_redis_credentials("", password="pw") == ""


def test_non_redis_scheme_is_untouched() -> None:
    """Someone pointing Celery at RabbitMQ must not have it rewritten."""
    amqp = "amqp://guest:guest@rabbit:5672//"
    assert apply_redis_credentials(amqp, password="pw") == amqp


def test_ipv6_host_is_bracketed() -> None:
    url = apply_redis_credentials("redis://[::1]:6379/0", password="pw")
    assert url == "redis://:pw@[::1]:6379/0"


def test_host_without_port() -> None:
    url = apply_redis_credentials("redis://redis/0", password="pw")
    assert url == "redis://:pw@redis/0"


# --------------------------------------------------------------------------
# Settings integration
# --------------------------------------------------------------------------

PRODUCTION_SECRETS = {
    "openai_api_key": "sk-real",
    "whatsapp_token": "EAAG-real",
    "whatsapp_phone_number_id": "123456",
    "whatsapp_verify_token": "verify-real",
    "whatsapp_app_secret": "app-secret-real",
    "admin_api_key": "admin-real",
}


def test_settings_inject_password_into_every_derived_url() -> None:
    """broker_url and result_backend must carry the password too.

    They are properties over redis_url, so a password injected into the field
    reaches Celery without either service definition mentioning it.
    """
    settings = Settings(
        environment="development",
        redis_url=BASE,
        redis_password="pw",
    )
    assert ":pw@" in settings.redis_url
    assert ":pw@" in settings.broker_url
    assert ":pw@" in settings.result_backend


def test_settings_inject_into_explicit_celery_urls() -> None:
    """Explicit broker/backend URLs are separate settings.

    Without this they would connect unauthenticated while redis_url looked
    correct -- the worker failing while the API appeared healthy.
    """
    settings = Settings(
        environment="development",
        redis_url=BASE,
        celery_broker_url="redis://redis:6379/1",
        celery_result_backend="redis://redis:6379/2",
        redis_password="pw",
    )
    assert settings.broker_url == "redis://:pw@redis:6379/1"
    assert settings.result_backend == "redis://:pw@redis:6379/2"


def test_production_requires_redis_auth() -> None:
    with pytest.raises(ValueError, match="Redis authentication"):
        Settings(
            environment="production",
            redis_url=BASE,
            redis_password="",
            **PRODUCTION_SECRETS,
        )


def test_production_accepts_password_embedded_in_url() -> None:
    """A managed Redis provider hands you one URL with credentials in it."""
    settings = Settings(
        environment="production",
        redis_url="redis://:inline@redis:6379/0",
        redis_password="",
        **PRODUCTION_SECRETS,
    )
    assert settings.redis_url == "redis://:inline@redis:6379/0"


def test_production_auth_requirement_can_be_waived() -> None:
    settings = Settings(
        environment="production",
        redis_url=BASE,
        redis_password="",
        redis_auth_required=False,
        **PRODUCTION_SECRETS,
    )
    assert settings.redis_url == BASE


def test_development_does_not_require_redis_auth() -> None:
    """Local development must keep working with no password at all."""
    settings = Settings(environment="development", redis_url=BASE)
    assert settings.redis_url == BASE
