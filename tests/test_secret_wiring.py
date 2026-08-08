"""The Compose secret list and init-secrets.sh must not drift apart.

CI claimed to check this and did not. The Docker job wrote a hard-coded list
of placeholder files so that `docker compose config -q` would evaluate, and
its comment said that "doubles as a check that the secrets list in the
compose file and init-secrets.sh agree". It does not: `config -q` never
inspects whether a declared secret's file exists. The two lists had drifted
apart by four names -- fcm_credentials and the three alert_* credentials --
while the job stayed green.

This is the real gate, and it lives in the Tests job where a mismatch fails
the build. It needs neither Docker nor a database.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "docker-compose.prod.yml"
INIT_SCRIPT = REPO_ROOT / "scripts" / "init-secrets.sh"
ALERTMANAGER_TEMPLATE = REPO_ROOT / "monitoring" / "alertmanager.yml.tmpl"

# Read by Alertmanager itself and by nothing else in the stack.
ALERT_SECRETS = (
    "alert_smtp_password",
    "alert_slack_webhook_url",
    "alert_telegram_bot_token",
)

# Named explicitly as well as covered by the sweep below, so that renaming a
# service cannot quietly turn the sweep into a test of nothing.
CREDENTIAL_FREE = ("app", "worker", "beat", "migrate", "alertmanager-config")

# Every helper in init-secrets.sh that brings a secret into existence. The
# name is always the first bare word after the call; inside the function
# bodies the argument is "$1" or "$name", which cannot match.
_INITIALISES = re.compile(
    r"\b(?:generate_secret|placeholder_secret|prompt_secret|write_secret"
    r"|skip_existing)\s+([a-z][a-z0-9_]*)\b"
)


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))


def _declared(compose: dict[str, Any]) -> set[str]:
    return set(compose.get("secrets") or {})


def _initialised() -> set[str]:
    names: set[str] = set()
    for line in INIT_SCRIPT.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        names.update(match.group(1) for match in _INITIALISES.finditer(line))
    return names


def _mounted(service: dict[str, Any]) -> set[str]:
    """The secrets one service actually receives.

    Compose allows a bare name or a mapping with a source. This file uses the
    short form, but a later edit switching to the long one must not silently
    empty this set.
    """
    mounted: set[str] = set()
    for entry in service.get("secrets") or []:
        mounted.add(entry if isinstance(entry, str) else entry.get("source"))
    return mounted


def test_every_declared_secret_is_initialised(compose: dict[str, Any]) -> None:
    declared = _declared(compose)
    initialised = _initialised()

    assert declared, "docker-compose.prod.yml declares no secrets at all"
    assert initialised, "no secret names parsed out of scripts/init-secrets.sh"
    assert declared == initialised, (
        f"declared but never created: {sorted(declared - initialised)}; "
        f"created but never declared: {sorted(initialised - declared)}"
    )


def test_a_missing_secret_would_be_noticed(compose: dict[str, Any]) -> None:
    """The comparison above has to be sensitive, not vacuous.

    A validator that parsed nothing, or that compared two empty sets, would
    pass just as happily. Removing any single name has to break the match --
    which is the mutation the previous CI check could not survive.
    """
    declared = _declared(compose)
    initialised = _initialised()

    for name in sorted(declared):
        assert declared - {name} != initialised


def test_alert_credentials_are_declared_and_created(compose: dict[str, Any]) -> None:
    alerts = set(ALERT_SECRETS)

    assert alerts <= _declared(compose)
    assert alerts <= _initialised()


def test_only_alertmanager_mounts_alert_secrets(compose: dict[str, Any]) -> None:
    """The credentials reach Alertmanager and nothing else.

    Every other service in the file is swept, not just the ones named above,
    so a new service that mounts a credential fails here rather than at
    somebody's next `docker inspect`.
    """
    services = compose["services"]
    alerts = set(ALERT_SECRETS)

    for name in CREDENTIAL_FREE:
        assert name in services, f"{name} is no longer a service; fix this test"

    assert alerts <= _mounted(services["alertmanager"])

    for name, service in services.items():
        if name == "alertmanager":
            continue
        leaked = alerts & _mounted(service)
        assert not leaked, f"{name} receives {sorted(leaked)}"


def test_each_secret_is_backed_by_a_file(compose: dict[str, Any]) -> None:
    """A declared secret whose file is not ./secrets/<name> is a rename bug."""
    for name, spec in (compose.get("secrets") or {}).items():
        assert (spec or {}).get("file", "").endswith(f"/{name}"), name


def test_alertmanager_reads_credentials_from_files() -> None:
    """Consumed as *_file, so the rendered config holds paths, not values."""
    template = ALERTMANAGER_TEMPLATE.read_text(encoding="utf-8")

    assert "smtp_auth_password_file: /run/secrets/alert_smtp_password" in template
    assert "api_url_file: /run/secrets/alert_slack_webhook_url" in template
    assert "bot_token_file: /run/secrets/alert_telegram_bot_token" in template

    for inline in ("smtp_auth_password:", "api_url:", "bot_token:"):
        assert inline not in template, f"{inline} would inline a credential"
