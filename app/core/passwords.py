"""Password hashing for operator accounts.

Uses ``hashlib.scrypt`` from the standard library rather than bcrypt, argon2
or passlib, for a reason that comes down to this repository's own
conventions. Every dependency here is pinned and swept by ``pip-audit`` in
CI, and a password hash is the last thing that should be blocked on a
transitive advisory in a library whose job is to call one KDF. scrypt is
memory-hard, has shipped with CPython since 3.6, and needs no wheel.

The encoded form is self-describing::

    scrypt$16384$8$1$<salt hex>$<derived key hex>

so the cost parameters travel with each hash. Raising them later does not
invalidate existing rows: :func:`verify_password` reads whatever parameters
the row was written with, and a caller that cares can re-hash on the next
successful login.
"""

import hashlib
import hmac
import secrets

# 128 * N * r bytes per verification, so ~16 MiB at these values. Chosen to
# sit under OpenSSL's default 32 MiB scrypt memory ceiling -- raising N to
# 2**15 doubles that and fails with "memory limit exceeded" unless maxmem is
# also raised, which is a footgun worth avoiding in a login path.
_N = 2**14
_R = 8
_P = 1
_SALT_BYTES = 16
_KEY_BYTES = 32
_SCHEME = "scrypt"

# What a row carries when the account must never authenticate interactively.
# No password can produce this: every real hash begins with the scheme name
# and splits into six "$"-separated fields, so verify_password rejects this
# value on the split before any comparison happens.
UNUSABLE_PASSWORD_HASH = "!"


def hash_password(password: str) -> str:
    """Hash a plaintext password into its self-describing encoded form."""
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = _derive(password, salt, _N, _R, _P)
    return f"{_SCHEME}${_N}${_R}${_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Check ``password`` against an encoded hash in constant time.

    Returns False rather than raising for every malformed input -- an empty
    hash, a truncated column, a scheme this build does not know. A caller
    deciding whether to admit somebody wants one answer, and "this row is
    unreadable" has to fall on the deny side of it.
    """
    try:
        scheme, n, r, p, salt_hex, key_hex = encoded.split("$")
    except ValueError:
        return False
    if scheme != _SCHEME:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(key_hex)
        derived = _derive(password, salt, int(n), int(r), int(p))
    except ValueError:
        return False
    return hmac.compare_digest(derived, expected)


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, dklen=_KEY_BYTES
    )
