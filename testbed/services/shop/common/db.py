"""Database access with an explicit connection checkout.

Every query runs inside `async with db.connection() as conn:` so the pool is used the way real
services use it: a request checks out a connection, works, and returns it. Pool waits, pending
checkouts and timeouts are measured here (exported as metrics in T3).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from shop.common.errors import ServiceUnavailableError
from shop.common.settings import ServiceSettings


class PoolStats:
    """In-process pool counters, read by the metrics layer."""

    def __init__(self) -> None:
        self.pending = 0
        self.timeouts = 0
        self.wait_seconds: list[float] = []  # drained by the metrics exporter

    def record_wait(self, seconds: float) -> None:
        self.wait_seconds.append(seconds)
        if len(self.wait_seconds) > 10_000:  # never grow without bound if nobody drains
            del self.wait_seconds[:5_000]


class Database:
    def __init__(self, settings: ServiceSettings) -> None:
        self.engine: AsyncEngine = create_async_engine(
            settings.database_dsn(),
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_s,
            pool_pre_ping=True,
            connect_args={
                "timeout": 5,
                "server_settings": {
                    "application_name": settings.service_name,
                    "statement_timeout": str(settings.db_statement_timeout_ms),
                },
            },
        )
        self.pool_size = settings.db_pool_size + settings.db_max_overflow
        self.stats = PoolStats()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection]:
        self.stats.pending += 1
        start = time.perf_counter()
        try:
            conn = await self.engine.connect()
        except PoolTimeoutError as exc:
            self.stats.timeouts += 1
            raise ServiceUnavailableError(
                "Timed out waiting for a database connection from the pool."
            ) from exc
        finally:
            self.stats.pending -= 1
            self.stats.record_wait(time.perf_counter() - start)
        try:
            yield conn
        finally:
            await conn.close()

    def checked_out(self) -> int:
        pool = self.engine.pool
        checked = getattr(pool, "checkedout", None)
        return int(checked()) if callable(checked) else 0

    async def check(self) -> None:
        """Fail fast at startup if the database is unreachable or misconfigured."""
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def close(self) -> None:
        await self.engine.dispose()
