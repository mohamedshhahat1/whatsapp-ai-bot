"""The create-admin bootstrap command.

The validation tests need no database and run everywhere the rest of the
suite skips for want of Postgres. The ones at the bottom create real rows.
"""

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.cli import (
    MIN_PASSWORD_LENGTH,
    CreateAdminError,
    create_admin,
    password_problem,
    username_problem,
)
from app.core.passwords import verify_password
from app.models.operator import LEGACY_OPERATOR_USERNAME, Operator

PASSWORD = "correct horse battery staple"


async def _purge_operator(session: AsyncSession, username: str) -> None:
    await session.execute(delete(Operator).where(Operator.username == username))
    await session.commit()


def test_an_empty_username_is_refused() -> None:
    assert username_problem("") is not None


def test_an_overlong_username_is_refused() -> None:
    assert username_problem("a" * 65) is not None


def test_the_reserved_legacy_username_is_refused() -> None:
    """Creating it would put a usable password on the shared-key identity."""
    assert username_problem(LEGACY_OPERATOR_USERNAME) is not None


def test_awkward_usernames_are_refused() -> None:
    assert username_problem("Mohamed") is not None
    assert username_problem("two words") is not None
    assert username_problem("-leading") is not None


def test_a_reasonable_username_is_accepted() -> None:
    assert username_problem("mohamed.shhahat") is None
    assert username_problem("op-1_2") is None


def test_a_short_password_is_refused() -> None:
    too_short = "a" * (MIN_PASSWORD_LENGTH - 1)
    assert password_problem(too_short, "op") is not None


def test_an_obvious_password_is_refused() -> None:
    assert password_problem("password1234", "op") is not None


def test_a_password_containing_the_username_is_refused() -> None:
    assert password_problem("mohamed-is-here", "mohamed") is not None


def test_a_password_of_few_distinct_characters_is_refused() -> None:
    """Long enough to pass the length bar, still not a password."""
    assert password_problem("ababababababab", "op") is not None


def test_a_reasonable_password_is_accepted() -> None:
    assert password_problem(PASSWORD, "op") is None


async def test_create_admin_stores_a_hashed_password(
    db: AsyncSession, requires_database: None
) -> None:
    username = "cli-admin-hashed"
    await _purge_operator(db, username)
    try:
        operator = await create_admin(db, username, PASSWORD, "CLI Admin")
        assert operator.is_admin
        assert operator.password_hash != PASSWORD
        assert verify_password(PASSWORD, operator.password_hash)
    finally:
        await _purge_operator(db, username)


async def test_a_duplicate_username_is_refused(
    db: AsyncSession, requires_database: None
) -> None:
    username = "cli-admin-duplicate"
    await _purge_operator(db, username)
    try:
        await create_admin(db, username, PASSWORD, "CLI Admin")
        with pytest.raises(CreateAdminError):
            await create_admin(db, username, PASSWORD, "CLI Admin")
    finally:
        await _purge_operator(db, username)


async def test_the_legacy_operator_cannot_be_recreated(
    db: AsyncSession, requires_database: None
) -> None:
    with pytest.raises(CreateAdminError):
        await create_admin(db, LEGACY_OPERATOR_USERNAME, PASSWORD, "Nope")


async def test_a_weak_password_never_reaches_the_database(
    db: AsyncSession, requires_database: None
) -> None:
    with pytest.raises(CreateAdminError):
        await create_admin(db, "cli-admin-weak", "short", "CLI Admin")
