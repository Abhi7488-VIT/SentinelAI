"""Password hashing with argon2id (argon2-cffi defaults = RFC 9106 low-memory profile).

Hashing is CPU/memory heavy (~64 MiB, tens of ms), so it runs in a worker thread to keep
the event loop responsive.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128

_hasher = PasswordHasher()


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return _hasher.hash("dummy-password-used-only-for-timing-equalization")


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


def _verify_sync(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except (VerificationError, InvalidHashError):
        return False


async def verify_password(password_hash: str, password: str) -> bool:
    return await asyncio.to_thread(_verify_sync, password_hash, password)


async def verify_against_dummy(password: str) -> None:
    """Spend the same work as a real verification (unknown-user login path)."""
    await asyncio.to_thread(_verify_sync, _dummy_hash(), password)


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
