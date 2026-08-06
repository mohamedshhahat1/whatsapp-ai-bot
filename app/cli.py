"""Command-line administration for operator accounts.

One command today: ``create-admin``. A freshly deployed instance has no way
in. Migration 0010 seeds only the reserved ``legacy-api-key`` row, and that
row carries the unusable password hash precisely so that nobody can log into
it, so the first real administrator has to come from somewhere. Before this,
the answer was "open a Python shell against production", which is not a
deployment step anyone should be asked to take.

    python -m app.cli create-admin

Inside Docker, where the environment is already configured::

    docker compose exec api python -m app.cli create-admin

The password is never accepted as a command-line argument. Arguments are
visible in shell history and in ``ps`` output to every other user on the
box, so it is prompted for, twice, with echo turned off.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import re
from collections.abc import Callable, Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.models.operator import LEGACY_OPERATOR_USERNAME, Operator
from app.services.auth_service import AuthService

# NIST SP 800-63B: length is the control that matters, and composition rules
# ("one capital, one symbol") mostly produce Passw0rd! and a sticky note.
# Twelve characters with no character-class requirement is the current
# recommendation rather than a compromise on one.
MIN_PASSWORD_LENGTH = 12

# operators.username is String(64) and operators.display_name is String(128).
# Checked here so that going over produces a sentence, rather than a driver
# DataError raised after the password has already been typed twice.
MAX_USERNAME_LENGTH = 64
MAX_DISPLAY_NAME_LENGTH = 128

# Usernames are compared exactly, so case-varying logins would be a support
# burden nobody asked for. Lowercase is enforced rather than folded, because
# silently changing what someone typed is worse than refusing it.
_USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

# Not a serious dictionary -- that is zxcvbn's job, and it is a dependency
# this does not need. These are the handful that actually turn up on a box
# whose admin account was created in a hurry during a deployment.
_OBVIOUS_PASSWORDS = frozenset(
    {
        "administrator",
        "changeme1234",
        "changeme12345",
        "letmein12345",
        "password1234",
        "qwertyuiop12",
        "123456789012",
    }
)

# Twelve identical characters clear the length bar without being a password.
_MIN_DISTINCT_CHARACTERS = 5


class CreateAdminError(Exception):
    """The administrator account could not be created."""


def username_problem(username: str) -> str | None:
    """Why this username is unacceptable, or None if it is fine."""
    if not username:
        return "The username cannot be empty."
    if len(username) > MAX_USERNAME_LENGTH:
        return f"The username cannot exceed {MAX_USERNAME_LENGTH} characters."
    if username == LEGACY_OPERATOR_USERNAME:
        return (
            f"{LEGACY_OPERATOR_USERNAME!r} is reserved for requests that "
            "authenticate with the shared ADMIN_API_KEY."
        )
    if not _USERNAME_PATTERN.match(username):
        return (
            "The username must be lowercase, start with a letter or digit, "
            "and use only letters, digits, dots, hyphens and underscores."
        )
    return None


def password_problem(password: str, username: str) -> str | None:
    """Why this password is unacceptable, or None if it is fine."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"The password must be at least {MIN_PASSWORD_LENGTH} characters."
    if password.lower() in _OBVIOUS_PASSWORDS:
        return "That password is among the first an attacker will try."
    if username and username.lower() in password.lower():
        return "The password must not contain the username."
    if len(set(password)) < _MIN_DISTINCT_CHARACTERS:
        return "The password does not use enough distinct characters."
    return None


async def create_admin(
    session: AsyncSession,
    username: str,
    password: str,
    display_name: str,
) -> Operator:
    """Create one administrator account.

    Validation happens here rather than only in the prompt loop, so that a
    deployment script calling this directly is refused for exactly the same
    reasons a person typing at a terminal is.
    """
    problem = username_problem(username)
    if problem is not None:
        raise CreateAdminError(problem)

    problem = password_problem(password, username)
    if problem is not None:
        raise CreateAdminError(problem)

    if len(display_name) > MAX_DISPLAY_NAME_LENGTH:
        raise CreateAdminError(
            f"The display name cannot exceed {MAX_DISPLAY_NAME_LENGTH} characters."
        )

    auth = AuthService(session)
    if await auth.get_by_username(username) is not None:
        raise CreateAdminError(f"An operator named {username!r} already exists.")

    try:
        return await auth.create_operator(
            username,
            password,
            display_name,
            is_admin=True,
        )
    except IntegrityError as exc:
        # Two people bootstrapping at once. The unique index is the real
        # guard; the check above only buys a friendlier message.
        await session.rollback()
        raise CreateAdminError(
            f"An operator named {username!r} already exists."
        ) from exc


def _prompt_username(ask: Callable[[str], str]) -> str:
    while True:
        username = ask("Username: ").strip()
        problem = username_problem(username)
        if problem is None:
            return username
        print(problem)


def _prompt_password(username: str, ask_password: Callable[[str], str]) -> str:
    while True:
        password = ask_password("Password: ")
        problem = password_problem(password, username)
        if problem is not None:
            print(problem)
            continue
        if password != ask_password("Confirm password: "):
            print("The passwords do not match.")
            continue
        return password


async def _create_admin_command(
    *,
    username: str | None,
    display_name: str | None,
    ask: Callable[[str], str],
    ask_password: Callable[[str], str],
) -> int:
    settings = get_settings()
    # NullPool and an explicit dispose: this is a one-shot process, and a
    # pooled connection left open would keep it alive after the work is done.
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        resolved = username if username is not None else _prompt_username(ask)
        problem = username_problem(resolved)
        if problem is not None:
            print(problem)
            return 1

        password = _prompt_password(resolved, ask_password)
        async with session_factory() as session:
            operator = await create_admin(
                session,
                resolved,
                password,
                display_name or resolved,
            )

        print(f"Created administrator {operator.username!r} (id {operator.id}).")
        print("Log in with POST /admin/auth/login to obtain a bearer token.")
        return 0
    except CreateAdminError as exc:
        print(str(exc))
        return 1
    except EOFError:
        # docker compose exec without -it, or a piped stdin that ran out.
        print("No input available; run this from an interactive terminal.")
        return 1
    except KeyboardInterrupt:
        print("Cancelled.")
        return 130
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Administrative commands for this deployment.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    create = subcommands.add_parser(
        "create-admin",
        help="Create an administrator account.",
        description=(
            "Create an administrator account. Anything not given as an "
            "option is prompted for. The password is always prompted for."
        ),
    )
    create.add_argument("--username", default=None)
    create.add_argument("--display-name", default=None)
    args = parser.parse_args(argv)

    return asyncio.run(
        _create_admin_command(
            username=args.username,
            display_name=args.display_name,
            ask=input,
            ask_password=getpass.getpass,
        )
    )


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
