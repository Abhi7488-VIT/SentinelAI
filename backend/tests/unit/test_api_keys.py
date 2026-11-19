"""Ingest API key primitives: format, parsing, hashing, constant-time verification."""

from __future__ import annotations

import hashlib

import pytest

from app.core.security.api_keys import (
    display_prefix,
    generate_key,
    hash_token,
    parse_key,
    verify_key,
)


def test_generated_key_format_and_hash() -> None:
    key = generate_key()
    scheme, prefix, secret = key.token.split("_", 2)
    assert scheme == "sai"
    assert prefix == key.prefix
    assert len(prefix) == 12
    assert int(prefix, 16) >= 0  # hex
    assert len(secret) == 43
    assert key.secret_hash == hashlib.sha256(key.token.encode()).digest()
    assert len(key.secret_hash) == 32
    assert key.token not in key.secret_hash.hex()


def test_keys_are_unique() -> None:
    keys = [generate_key() for _ in range(200)]
    assert len({k.token for k in keys}) == 200
    assert len({k.prefix for k in keys}) == 200


def test_parse_roundtrip_and_verify() -> None:
    key = generate_key()
    parsed = parse_key(key.token)
    assert parsed is not None
    assert parsed.prefix == key.prefix
    assert verify_key(parsed, key.secret_hash)


def test_secret_containing_underscores_parses() -> None:
    token = "sai_0123456789ab_" + "_" * 10 + "A" * 33
    parsed = parse_key(token)
    assert parsed is not None
    assert parsed.prefix == "0123456789ab"


def test_verify_rejects_other_key_with_same_prefix() -> None:
    real = generate_key()
    forged = f"sai_{real.prefix}_{'A' * 43}"
    parsed = parse_key(forged)
    assert parsed is not None
    assert not verify_key(parsed, real.secret_hash)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "sai_",
        "Bearer sai_0123456789ab_" + "A" * 43,
        "sai_0123456789AB_" + "A" * 43,  # uppercase prefix
        "sai_0123456789a_" + "A" * 43,  # short prefix
        "sai_0123456789ab_" + "A" * 42,  # short secret
        "sai_0123456789ab_" + "A" * 44,  # long secret
        "sk_0123456789ab_" + "A" * 43,  # wrong scheme
        "sai_0123456789ab_" + "A" * 42 + "=",  # padding / bad char
        " sai_0123456789ab_" + "A" * 43,
    ],
)
def test_parse_rejects_malformed(raw: str) -> None:
    assert parse_key(raw) is None


def test_hash_is_deterministic() -> None:
    assert hash_token("sai_x") == hash_token("sai_x")


def test_display_prefix_hides_secret() -> None:
    key = generate_key()
    shown = display_prefix(key.prefix)
    assert shown == f"sai_{key.prefix}_…"
    assert key.token.split("_", 2)[2] not in shown
