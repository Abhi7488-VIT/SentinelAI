"""JWT access tokens and refresh token primitives."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.security.tokens import (
    AccessTokenExpiredError,
    InvalidAccessTokenError,
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_refresh_token,
)
from tests.conftest import TEST_JWT_SECRET, make_settings

SETTINGS = make_settings()


def _claims(**overrides: object) -> dict[str, object]:
    now = datetime.now(UTC)
    base: dict[str, object] = {
        "sub": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "jti": "j",
        "iss": SETTINGS.jwt_issuer,
        "aud": SETTINGS.jwt_audience,
        "typ": "access",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def test_round_trip() -> None:
    user_id = uuid.uuid4()
    token = create_access_token(user_id, SETTINGS)
    assert token.expires_in == SETTINGS.access_token_ttl_seconds
    claims = decode_access_token(token.token, SETTINGS)
    assert claims.user_id == user_id
    assert claims.expires_at > claims.issued_at


def test_expired() -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    token = create_access_token(uuid.uuid4(), SETTINGS, now=past)
    with pytest.raises(AccessTokenExpiredError):
        decode_access_token(token.token, SETTINGS)


def test_tampered_signature() -> None:
    token = create_access_token(uuid.uuid4(), SETTINGS).token
    head, payload, sig = token.split(".")
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(f"{head}.{payload}.{flipped}", SETTINGS)


def test_wrong_secret() -> None:
    token = jwt.encode(_claims(), "another-secret-" + "z" * 40, algorithm="HS256")
    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(token, SETTINGS)


def test_alg_none_rejected() -> None:
    token = jwt.encode(_claims(), key="", algorithm="none")
    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(token, SETTINGS)


def test_hs512_rejected_even_with_right_secret() -> None:
    token = jwt.encode(_claims(), TEST_JWT_SECRET + "x" * 8, algorithm="HS512")
    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(token, SETTINGS)


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "someone-else"},
        {"iss": "evil"},
        {"typ": "refresh"},
        {"sub": "not-a-uuid"},
        {"jti": None},
        {"exp": None},
    ],
)
def test_bad_claims_rejected(overrides: dict[str, object]) -> None:
    token = jwt.encode(_claims(**overrides), TEST_JWT_SECRET, algorithm="HS256")
    with pytest.raises(InvalidAccessTokenError):
        decode_access_token(token, SETTINGS)


def test_garbage_rejected() -> None:
    with pytest.raises(InvalidAccessTokenError):
        decode_access_token("not.a.jwt", SETTINGS)


def test_refresh_tokens_random_and_hashed() -> None:
    a, b = generate_refresh_token(), generate_refresh_token()
    assert a != b
    assert len(a) >= 43
    assert hash_refresh_token(a) == hash_refresh_token(a)
    assert hash_refresh_token(a) != hash_refresh_token(b)
    assert len(hash_refresh_token(a)) == 32
