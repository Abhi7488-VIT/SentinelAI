"""user-service: users, login and sessions (sessions live in Redis)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select

from shop.common.app import create_service_app
from shop.common.db import Database
from shop.common.errors import NotFoundError, ServiceUnavailableError, UnauthorizedError
from shop.common.settings import ServiceSettings
from shop.user_service.db import users
from shop.user_service.security import new_session_token, verify_password

SESSION_TTL_S = 3600

logger = logging.getLogger("shop.users")


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=200)


class SessionOut(BaseModel):
    token: str
    user_id: int


class SessionUser(BaseModel):
    user_id: int


class UserOut(BaseModel):
    id: int
    email: str
    name: str


def get_db(request: Request) -> Database:
    db: Database = request.app.state.db
    return db


def get_redis(request: Request) -> Redis:
    redis: Redis = request.app.state.redis
    return redis


DbDep = Annotated[Database, Depends(get_db)]
RedisDep = Annotated[Redis, Depends(get_redis)]


def create_app() -> FastAPI:
    settings = ServiceSettings.for_service("user-service")
    if settings.redis_url is None:
        raise RuntimeError("REDIS_URL is required")
    redis_url = settings.redis_url.get_secret_value()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = Database(settings)
        db.instrument(app.state.telemetry)
        await db.check()  # a misconfigured database fails the start, as it should
        app.state.db = db
        app.state.redis = Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        yield
        await app.state.redis.aclose()
        await db.close()

    app = create_service_app(settings, lifespan)

    @app.post("/login", response_model=SessionOut)
    async def login(body: LoginIn, db: DbDep, redis: RedisDep) -> SessionOut:
        async with db.connection() as conn:
            row = (
                await conn.execute(
                    select(users.c.id, users.c.password_hash).where(
                        users.c.email == body.email.lower()
                    )
                )
            ).first()
        if row is None or not verify_password(body.password, row.password_hash):
            logger.warning("sign-in failed: invalid credentials")
            raise UnauthorizedError("Invalid email or password.")
        token = new_session_token()
        try:
            await redis.set(f"session:{token}", row.id, ex=SESSION_TTL_S)
        except RedisError as exc:
            raise ServiceUnavailableError("Session store unavailable.") from exc
        logger.info("user signed in", extra={"user.id": row.id})
        return SessionOut(token=token, user_id=row.id)

    @app.get("/sessions/{token}", response_model=SessionUser)
    async def session(token: str, redis: RedisDep) -> SessionUser:
        try:
            user_id = await redis.get(f"session:{token}")
        except RedisError as exc:
            raise ServiceUnavailableError("Session store unavailable.") from exc
        if user_id is None:
            raise NotFoundError("Unknown or expired session.")
        return SessionUser(user_id=int(user_id))

    @app.get("/users/{user_id}", response_model=UserOut)
    async def get_user(user_id: int, db: DbDep) -> UserOut:
        async with db.connection() as conn:
            row = (
                await conn.execute(
                    select(users.c.id, users.c.email, users.c.name).where(users.c.id == user_id)
                )
            ).first()
        if row is None:
            raise NotFoundError(f"No user {user_id}.")
        return UserOut(id=row.id, email=row.email, name=row.name)

    return app
