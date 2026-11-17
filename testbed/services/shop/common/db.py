"""Database access with an explicit connection checkout.

Every query runs inside `async with db.connection() as conn:` so the pool is used the way real
services use it: a request checks out a connection, works, and returns it. Checkout waits,
pending checkouts and pool timeouts are measured here and exported as the semantic-convention
connection-pool metrics (db.client.connection.*).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager

from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.metrics import CallbackOptions, Observation
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from shop.common.errors import ServiceUnavailableError
from shop.common.settings import ServiceSettings
from shop.common.telemetry import Telemetry


class Database:
    def __init__(self, settings: ServiceSettings) -> None:
        self.pool_name = settings.service_name
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
        self.max_connections = settings.db_pool_size + settings.db_max_overflow
        self.pending = 0
        self.timeouts = 0
        self._on_wait: Callable[[float], None] | None = None

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection]:
        self.pending += 1
        start = time.perf_counter()
        try:
            conn = await self.engine.connect()
        except PoolTimeoutError as exc:
            self.timeouts += 1
            raise ServiceUnavailableError(
                "Timed out waiting for a database connection from the pool."
            ) from exc
        finally:
            self.pending -= 1
            if self._on_wait is not None:
                self._on_wait(time.perf_counter() - start)
        try:
            yield conn
        finally:
            await conn.close()

    def checked_out(self) -> int:
        checked = getattr(self.engine.pool, "checkedout", None)
        return int(checked()) if callable(checked) else 0

    def instrument(self, telemetry: Telemetry) -> None:
        """DB client spans + connection-pool metrics."""
        if not telemetry.enabled:
            return
        SQLAlchemyInstrumentor().instrument(
            engine=self.engine.sync_engine,
            tracer_provider=telemetry.tracer_provider,
            meter_provider=telemetry.meter_provider,
            enable_commenter=False,
        )
        meter = telemetry.meter("shop.db")
        attrs = {"db.client.connection.pool.name": self.pool_name}

        def count(_: CallbackOptions) -> Iterable[Observation]:
            used = self.checked_out()
            idle = (
                max(0, self.engine.pool.checkedin())
                if hasattr(self.engine.pool, "checkedin")
                else 0
            )
            return [
                Observation(used, {**attrs, "db.client.connection.state": "used"}),
                Observation(idle, {**attrs, "db.client.connection.state": "idle"}),
            ]

        meter.create_observable_up_down_counter(
            "db.client.connection.count",
            [count],
            unit="{connection}",
            description="Connections in the pool by state.",
        )
        meter.create_observable_up_down_counter(
            "db.client.connection.max",
            [lambda _: [Observation(self.max_connections, attrs)]],
            unit="{connection}",
            description="Maximum connections the pool allows.",
        )
        meter.create_observable_up_down_counter(
            "db.client.connection.pending_requests",
            [lambda _: [Observation(self.pending, attrs)]],
            unit="{request}",
            description="Requests waiting for a connection.",
        )
        meter.create_observable_counter(
            "db.client.connection.timeouts",
            [lambda _: [Observation(self.timeouts, attrs)]],
            unit="{timeout}",
            description="Checkouts that timed out waiting for a connection.",
        )
        wait = meter.create_histogram(
            "db.client.connection.wait_time",
            unit="s",
            description="Time to obtain a connection from the pool.",
        )
        self._on_wait = lambda seconds: wait.record(seconds, attrs)

    async def check(self) -> None:
        """Fail fast at startup if the database is unreachable or misconfigured."""
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def close(self) -> None:
        await self.engine.dispose()
