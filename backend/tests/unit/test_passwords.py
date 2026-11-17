"""argon2id hashing."""

from __future__ import annotations

from argon2 import PasswordHasher

from app.core.security.passwords import (
    hash_password,
    needs_rehash,
    verify_against_dummy,
    verify_password,
)


async def test_hash_is_argon2id_and_salted() -> None:
    h1 = await hash_password("correct horse battery staple")
    h2 = await hash_password("correct horse battery staple")
    assert h1.startswith("$argon2id$")
    assert h1 != h2  # random salt


async def test_verify_correct_and_wrong() -> None:
    h = await hash_password("correct horse battery staple")
    assert await verify_password(h, "correct horse battery staple")
    assert not await verify_password(h, "correct horse battery stapler")


async def test_verify_malformed_hash_is_false_not_error() -> None:
    assert not await verify_password("not-a-hash", "whatever")


async def test_dummy_verification_does_not_raise() -> None:
    await verify_against_dummy("anything")


async def test_needs_rehash_when_parameters_weaker() -> None:
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("pw-for-rehash-test")
    assert needs_rehash(weak)
    assert not needs_rehash(await hash_password("pw-for-rehash-test"))
