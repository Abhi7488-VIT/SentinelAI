"""Dependency probes used by the readiness check. Each raises on failure."""

from __future__ import annotations

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


async def ping_postgres(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def ping_redis(redis: Redis) -> None:
    if not await redis.ping():
        raise ConnectionError("Redis PING returned a falsy reply")
