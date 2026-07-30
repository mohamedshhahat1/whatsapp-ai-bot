"""Cross-file invariants that nothing else can enforce.

Most of this sprint's correctness lives inside one module and is tested there.
Three properties do not: they are relationships between numbers that sit in
different files, are never compared at runtime, and whose violation produces
intermittent duplicate replies in production rather than an error anywhere.

  * visibility_timeout MUST exceed task_time_limit, or Redis hands a slow task
    to a second worker while the first is still running it.
  * the worker's stop_grace_period MUST exceed task_time_limit, or Docker
    SIGKILLs mid-task on every deploy -- and with acks_late that means
    redelivery.
  * nginx must not serve the application over plain HTTP.

A test is the only place a constraint like that can be written down somewhere
that will tell someone they broke it.
"""

from pathlib import Path

import pytest

from app.config import Settings
from app.workers.celery_app import celery_app

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
COMPOSE = REPOSITORY_ROOT / "docker-compose.prod.yml"
NGINX_TEMPLATE = REPOSITORY_ROOT / "nginx" / "templates" / "default.conf.template"
TLS_CONF = REPOSITORY_ROOT / "nginx" / "tls.conf"
HEADERS_CONF = REPOSITORY_ROOT / "nginx" / "security-headers.conf"


# ---------------------------------------------------------------------------
# Celery durability
# ---------------------------------------------------------------------------


def test_tasks_are_acknowledged_only_after_they_finish() -> None:
    """Without this a worker killed mid-task loses the customer's message."""
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True


def test_workers_do_not_hoard_messages() -> None:
    """Prefetching parks messages in a worker's memory where a crash loses them,
    and starves other workers while one is busy."""
    assert celery_app.conf.worker_prefetch_multiplier == 1


def test_the_redelivery_window_cannot_overlap_a_running_task() -> None:
    """THE duplicate-reply invariant.

    Redis has no broker-side ack, so kombu re-delivers anything not finished
    within visibility_timeout -- while the original is still running. With
    acks_late that window is the whole task. Keeping the hard time limit
    strictly below it means a task is always killed before it can be
    redelivered, so the overlap is unreachable rather than merely unlikely.
    """
    visibility = celery_app.conf.broker_transport_options["visibility_timeout"]
    hard_limit = celery_app.conf.task_time_limit

    assert hard_limit is not None
    assert visibility > hard_limit, (
        f"visibility_timeout ({visibility}s) must exceed task_time_limit "
        f"({hard_limit}s), or slow messages are processed twice and customers "
        "are answered twice."
    )


def test_the_soft_limit_leaves_room_to_clean_up() -> None:
    soft = celery_app.conf.task_soft_time_limit
    hard = celery_app.conf.task_time_limit
    assert soft < hard, "the soft limit must fire before the thread is killed"


def test_a_wedged_task_cannot_hold_a_thread_forever() -> None:
    assert celery_app.conf.task_time_limit is not None
    assert celery_app.conf.task_soft_time_limit is not None


# ---------------------------------------------------------------------------
# Deploy safety: compose and Celery must agree
# ---------------------------------------------------------------------------


def _worker_stop_grace_period() -> int:
    """Read stop_grace_period from the worker service.

    Parsed textually rather than with a YAML library: compose is not a test
    dependency, and the file contains ${VAR:?...} interpolation that a strict
    parser has no opinion about.
    """
    text = COMPOSE.read_text(encoding="utf-8")
    worker_section = text.split("\n  worker:", 1)
    assert len(worker_section) == 2, "no worker service in docker-compose.prod.yml"
    # Stop at the next top-level service definition.
    body = worker_section[1].split("\n  db:", 1)[0]

    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("stop_grace_period:"):
            value = stripped.split(":", 1)[1].strip()
            return int(value.rstrip("s"))
    raise AssertionError("the worker has no stop_grace_period")


def test_the_worker_is_given_time_to_drain_on_deploy() -> None:
    """Docker's default is 10 seconds, which SIGKILLs work mid-completion.

    With task_acks_late, a killed task is redelivered -- and a redelivery that
    lands after a WhatsApp send is a duplicate reply and a second OpenAI
    charge. The grace period has to outlast the longest task we permit.
    """
    grace = _worker_stop_grace_period()
    hard_limit = celery_app.conf.task_time_limit

    assert grace > hard_limit, (
        f"stop_grace_period ({grace}s) must exceed task_time_limit "
        f"({hard_limit}s), or every deploy kills in-flight customer messages."
    )


def test_settings_defaults_satisfy_the_same_invariants() -> None:
    """The defaults must be safe for anyone who never sets these variables."""
    settings = Settings()
    assert settings.celery_visibility_timeout > settings.celery_task_time_limit
    assert settings.celery_task_soft_time_limit < settings.celery_task_time_limit


# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------


def test_plain_http_only_serves_the_acme_challenge() -> None:
    """Everything on port 80 is redirected except the ACME challenge path.

    The exception is not optional: certbot's HTTP-01 validation is fetched over
    plain HTTP, and redirecting it breaks renewal. Which then fails silently
    for 90 days.
    """
    config = NGINX_TEMPLATE.read_text(encoding="utf-8")
    assert "listen 80" in config
    assert "/.well-known/acme-challenge/" in config
    assert "return 301 https://" in config


def test_tls_is_terminated_on_443() -> None:
    config = NGINX_TEMPLATE.read_text(encoding="utf-8")
    assert "listen 443 ssl" in config


def test_only_modern_tls_versions_are_offered() -> None:
    tls = TLS_CONF.read_text(encoding="utf-8")
    assert "ssl_protocols TLSv1.2 TLSv1.3;" in tls
    for dead in ("TLSv1;", "TLSv1.1", "SSLv3", "SSLv2"):
        assert dead not in tls, f"{dead} must not be offered"


@pytest.mark.parametrize(
    "header",
    [
        "Strict-Transport-Security",
        "Content-Security-Policy",
        "X-Frame-Options",
        "X-Content-Type-Options",
        "Referrer-Policy",
        "Permissions-Policy",
    ],
)
def test_security_header_is_set(header: str) -> None:
    assert header in HEADERS_CONF.read_text(encoding="utf-8")


def test_hsts_is_long_lived_and_covers_subdomains() -> None:
    """A short max-age provides almost no protection; browsers forget too soon."""
    headers = HEADERS_CONF.read_text(encoding="utf-8")
    assert "max-age=63072000" in headers
    assert "includeSubDomains" in headers


def test_security_headers_are_always_sent() -> None:
    """Without `always`, nginx omits these on error responses -- the 4xx and 5xx
    pages where clickjacking and sniffing protections matter most."""
    for line in HEADERS_CONF.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("add_header"):
            assert line.rstrip().endswith("always;"), line


def test_websockets_are_proxied_with_a_long_timeout() -> None:
    """The dashboard stream is idle between events; the 60s default would drop
    it roughly every minute and the UI would flap."""
    config = NGINX_TEMPLATE.read_text(encoding="utf-8")
    assert "/ws/" in config
    assert "proxy_set_header Upgrade" in config
    assert "proxy_read_timeout 3600s" in config


def test_metrics_are_not_reachable_from_outside() -> None:
    config = NGINX_TEMPLATE.read_text(encoding="utf-8")
    assert "location /metrics" in config


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------


def test_the_stack_runs_a_backup_service() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert "\n  backup:" in text
    assert "backup-scheduler.sh" in text


def test_the_backup_healthcheck_measures_output_not_liveness() -> None:
    """A backup container running happily while producing nothing for three days
    is the failure that matters, and a liveness probe cannot see it."""
    text = COMPOSE.read_text(encoding="utf-8")
    assert "backup-healthcheck.sh" in text


def test_every_backup_script_is_executable() -> None:
    """They are container entrypoints and healthchecks; a missing exec bit fails
    at runtime with an opaque error rather than at review time."""
    import os
    import stat

    scripts = [
        "backup.sh",
        "backup-scheduler.sh",
        "backup-healthcheck.sh",
        "restore.sh",
        "verify-restore.sh",
        "init-letsencrypt.sh",
    ]
    for name in scripts:
        path = REPOSITORY_ROOT / "scripts" / name
        assert path.exists(), f"scripts/{name} is missing"
        mode = os.stat(path).st_mode
        assert mode & stat.S_IXUSR, f"scripts/{name} is not executable"
