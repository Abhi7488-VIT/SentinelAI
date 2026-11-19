"""Per-process async runtime for hot-path Celery tasks (the ingest writer).

Each prefork worker process lazily creates one event loop and one small connection pool, reused
across tasks; they are recreated after a fork (pid check), never shared between processes.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.session import create_engine, create_sessionmaker


@dataclass
class _Runtime:
    pid: int
    loop: asyncio.AbstractEventLoop
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]


_runtime: _Runtime | None = None


def _get(settings: Settings, application_name: str) -> _Runtime:
    global _runtime
    if _runtime is None or _runtime.pid != os.getpid():
        loop = asyncio.new_event_loop()
        small = settings.model_copy(update={"db_pool_size": 2, "db_max_overflow": 2})
        engine = create_engine(small, application_name=application_name)
        _runtime = _Runtime(os.getpid(), loop, engine, create_sessionmaker(engine))
    return _runtime


def run_in_session[T](
    settings: Settings, work: Callable[[AsyncSession], Awaitable[T]], *, application_name: str
) -> T:
    rt = _get(settings, application_name)

    async def _main() -> T:
        async with rt.sessionmaker() as session:
            return await work(session)

    return rt.loop.run_until_complete(_main())
