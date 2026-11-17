"""Shared FastAPI dependencies: settings, DB session, request metadata, authentication."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ForbiddenError
from app.core.rate_limit import RateLimiter
from app.core.request_context import current_request_id
from app.core.security.tokens import InvalidAccessTokenError, decode_access_token
from app.db.session import get_session
from app.models.user import User
from app.repositories.users import UserRepository
from app.services.audit import RequestMeta
from app.services.auth import AuthService

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


def get_redis(request: Request) -> Redis:
    redis: Redis = request.app.state.redis
    return redis


def get_request_meta(request: Request) -> RequestMeta:
    # The direct peer address. X-Forwarded-For is not trusted (no trusted proxy configured).
    ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    return RequestMeta(
        ip=ip,
        user_agent=user_agent[:512] if user_agent else None,
        request_id=current_request_id(),
    )


RequestMetaDep = Annotated[RequestMeta, Depends(get_request_meta)]


def get_rate_limiter(redis: Annotated[Redis, Depends(get_redis)]) -> RateLimiter:
    return RateLimiter(redis)


RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]


def get_auth_service(
    session: SessionDep, settings: SettingsDep, limiter: RateLimiterDep
) -> AuthService:
    return AuthService(session, settings, limiter)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


async def get_current_user(request: Request, session: SessionDep, settings: SettingsDep) -> User:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise InvalidAccessTokenError("Authentication required.")
    claims = decode_access_token(token.strip(), settings)
    user = await UserRepository(session).get(claims.user_id)
    if user is None or not user.is_active:
        raise InvalidAccessTokenError()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def verify_origin(request: Request, settings: SettingsDep) -> None:
    """For cookie-authenticated endpoints: a browser Origin must be allow-listed.

    Requests without Origin (non-browser clients) are allowed; SameSite=Strict already keeps
    browsers from attaching the cookie to cross-site requests.
    """
    origin = request.headers.get("origin")
    if origin is not None and origin not in settings.cors_origins:
        raise ForbiddenError("Origin not allowed.", code="origin_not_allowed")
