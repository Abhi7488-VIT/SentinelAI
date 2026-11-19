"""Run async database work from synchronous Celery tasks.

Each call uses its own short-lived engine (NullPool) inside `asyncio.run`, so no connection or
event loop is shared across task invocations or forked worker processes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings


def run_with_session[T](
    settings: Settings, work: Callable[[AsyncSession], Awaitable[T]], *, application_name: str
) -> T:
    async def _main() -> T:
        engine = create_async_engine(
            settings.database_url.get_secret_value(),
            poolclass=NullPool,
            connect_args={
                "timeout": 5,
                "server_settings": {
                    "application_name": application_name,
                    "statement_timeout": str(settings.db_statement_timeout_ms),
                    "jit": "off",  # see app/db/session.py
                },
            },
        )
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                return await work(session)
        finally:
            await engine.dispose()

    return asyncio.run(_main())
