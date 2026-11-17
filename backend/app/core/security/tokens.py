"""Access tokens (JWT, HS256) and opaque refresh tokens. See ADR 0001."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from app.core.config import Settings
from app.core.errors import AppError

ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access"  # noqa: S105  (token type label, not a secret)
_LEEWAY_SECONDS = 10


class InvalidAccessTokenError(AppError):
    status_code = 401
    code = "not_authenticated"
    message = "Invalid or missing access token."


class AccessTokenExpiredError(AppError):
    status_code = 401
    code = "token_expired"
    message = "Access token expired."


@dataclass(frozen=True, slots=True)
class AccessToken:
    token: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    user_id: uuid.UUID
    jti: str
    issued_at: datetime
    expires_at: datetime


def create_access_token(
    user_id: uuid.UUID, settings: Settings, *, now: datetime | None = None
) -> AccessToken:
    now = now or datetime.now(UTC)
    ttl = settings.access_token_ttl_seconds
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl)).timestamp()),
        "jti": uuid.uuid4().hex,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "typ": ACCESS_TOKEN_TYPE,
    }
    token = jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=ALGORITHM)
    return AccessToken(token=token, expires_in=ttl)


def decode_access_token(token: str, settings: Settings) -> AccessTokenClaims:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[ALGORITHM],  # pinned: rejects "none" and algorithm-confusion attempts
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            leeway=_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "sub", "jti", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AccessTokenExpiredError() from exc
    except jwt.PyJWTError as exc:
        raise InvalidAccessTokenError() from exc

    if payload.get("typ") != ACCESS_TOKEN_TYPE:
        raise InvalidAccessTokenError()
    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except ValueError as exc:
        raise InvalidAccessTokenError() from exc
    return AccessTokenClaims(
        user_id=user_id,
        jti=str(payload["jti"]),
        issued_at=datetime.fromtimestamp(int(payload["iat"]), UTC),
        expires_at=datetime.fromtimestamp(int(payload["exp"]), UTC),
    )


def generate_refresh_token() -> str:
    """256 bits of randomness, URL-safe. Only its hash is ever stored."""
    return secrets.token_urlsafe(32)


def hash_refresh_token(token: str) -> bytes:
    # A fast hash is appropriate: the token is high-entropy random, not a password.
    return hashlib.sha256(token.encode("utf-8")).digest()
