"""Password hashing.

No database and no fixtures, so these run everywhere the rest of the suite
skips for want of Postgres.
"""

from app.core.passwords import (
    UNUSABLE_PASSWORD_HASH,
    hash_password,
    verify_password,
)


def test_a_password_verifies_against_its_own_hash() -> None:
    encoded = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", encoded)


def test_a_wrong_password_does_not_verify() -> None:
    encoded = hash_password("correct horse battery staple")
    assert not verify_password("Correct horse battery staple", encoded)


def test_the_same_password_hashes_differently_each_time() -> None:
    """Distinct salts, so equal passwords are not visibly equal in the table."""
    first = hash_password("same")
    second = hash_password("same")
    assert first != second
    assert verify_password("same", first)
    assert verify_password("same", second)


def test_the_encoding_carries_its_own_cost_parameters() -> None:
    """So the cost can be raised later without invalidating existing rows."""
    scheme, n, r, p, salt, key = hash_password("pw").split("$")
    assert scheme == "scrypt"
    assert int(n) >= 2**14
    assert int(r) > 0
    assert int(p) > 0
    # 16 salt bytes and a 32 byte key, hex encoded.
    assert len(salt) == 32
    assert len(key) == 64


def test_the_unusable_hash_never_verifies() -> None:
    """The sentinel on accounts that must not be logged into.

    The reserved legacy-api-key operator carries this, so nothing here may
    depend on knowing a password -- the empty string included.
    """
    assert not verify_password("", UNUSABLE_PASSWORD_HASH)
    assert not verify_password("!", UNUSABLE_PASSWORD_HASH)
    assert not verify_password("anything", UNUSABLE_PASSWORD_HASH)


def test_malformed_hashes_are_refused_rather_than_raising() -> None:
    """A caller deciding whether to admit somebody wants one answer.

    An unreadable column has to fall on the deny side of it, not surface as
    a 500 that says the row is broken.
    """
    for encoded in (
        "",
        "not-a-hash",
        "scrypt$16384$8$1$deadbeef",
        "scrypt$16384$8$1$nothex$nothex",
        "bcrypt$16384$8$1$aabb$ccdd",
        "scrypt$notanumber$8$1$aabb$ccdd",
    ):
        assert not verify_password("pw", encoded)
