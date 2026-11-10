"""Password hashing for the app's own accounts (issue #44): scrypt from the standard library.

A stored hash is one plain line, `scrypt:<n>:<r>:<p>:<salt>:<key>` (salt and key in unpadded
URL-safe base64). It deliberately has no `$`: it goes through `.env` and Docker Compose, which
treat a dollar sign as the start of a variable. The cost parameters are in the hash, so they can
be raised later and old hashes still verify (`needs_rehash` says when to store a new one).

* `n=2**15, r=8, p=1` needs 32 MiB and about 80 ms: slow enough that guessing is expensive, cheap
  enough for a login on a small VM. `maxmem` is set explicitly (OpenSSL's default of 32 MiB is
  exactly the limit).
* Comparison is constant-time (`hmac.compare_digest`).
* `verify_password("x", None)` and a malformed hash still spend the same time and answer False,
  so an unknown user name cannot be told from a wrong password by timing or by an exception.
* A password is a string of at least `MIN_LENGTH` characters and at most `MAX_LENGTH` (a very long
  one would only be a way to make the server compute for a long time). It is never logged here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

N = 2**15
R = 8
P = 1
KEY_BYTES = 32
SALT_BYTES = 16
MIN_LENGTH = 12
MAX_LENGTH = 200
PREFIX = "scrypt"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def check_strength(password: str) -> str | None:
    """None if the password is acceptable, else a short English reason (the API translates)."""
    if not isinstance(password, str):
        return "the password must be text"
    if len(password) < MIN_LENGTH:
        return f"the password needs at least {MIN_LENGTH} characters"
    if len(password) > MAX_LENGTH:
        return f"the password may have at most {MAX_LENGTH} characters"
    if "\x00" in password:
        return "the password must not contain a null character"
    return None


def _derive(password: str, salt: bytes, n: int, r: int, p: int, length: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=length, maxmem=128 * n * r * 4
    )


def hash_password(password: str, *, n: int = N) -> str:
    """The stored form of a password (a new random salt every time)."""
    problem = check_strength(password)
    if problem:
        raise ValueError(problem)
    salt = secrets.token_bytes(SALT_BYTES)
    key = _derive(password, salt, n, R, P, KEY_BYTES)
    return f"{PREFIX}:{n}:{R}:{P}:{_b64(salt)}:{_b64(key)}"


def _parse(stored: str | None) -> tuple[int, int, int, bytes, bytes] | None:
    if not isinstance(stored, str):
        return None
    parts = stored.split(":")
    if len(parts) != 6 or parts[0] != PREFIX:
        return None
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, key = _unb64(parts[4]), _unb64(parts[5])
    except ValueError:
        return None
    if not (2**10 <= n <= 2**20 and n & (n - 1) == 0 and 1 <= r <= 16 and 1 <= p <= 4):
        return None  # a corrupted or hostile value must not make the server allocate gigabytes
    if len(salt) < 8 or not 16 <= len(key) <= 64:
        return None
    return n, r, p, salt, key


# What an unknown user or a broken hash is checked against, so the time spent is the same.
_DUMMY = hash_password("not a real password, only for timing", n=N)


def verify_password(password: str, stored: str | None) -> bool:
    """True if `password` matches the stored hash. Never raises."""
    parsed = _parse(stored)
    target = parsed or _parse(_DUMMY)
    assert target is not None
    n, r, p, salt, key = target
    # A too long (or not text) password is not even tried against the real hash, but the time
    # spent stays the same.
    acceptable = isinstance(password, str) and len(password) <= MAX_LENGTH
    try:
        candidate = _derive(password if acceptable else "", salt, n, r, p, len(key))
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(candidate, key) and parsed is not None and acceptable


def needs_rehash(stored: str) -> bool:
    """Is this hash weaker than what new passwords get (so a login may store a fresh one)?"""
    parsed = _parse(stored)
    return parsed is None or (parsed[0], parsed[1], parsed[2]) != (N, R, P)


def is_hash(value: str | None) -> bool:
    """Does the text look like one of our hashes? (For settings: `changeme` and empty do not.)"""
    return _parse(value) is not None
