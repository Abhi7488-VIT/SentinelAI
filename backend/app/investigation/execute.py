"""Process-level wiring for the Celery task: engine, LLM client, checkpointer, then the runner."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.tenancy import SystemScope
from app.investigation.checkpoint import open_checkpointer
from app.investigation.runner import (
    CommittingUsageSink,
    RunOutcome,
    dead_letter,
    run_investigation,
)
from app.llm.factory import build_client
from app.repositories.investigations import stuck_investigations

# A released lease (task retry pending, countdown ≤ ~24 s) or a queued run older than this is
# considered lost and re-enqueued by the sweep; the lease makes a duplicate harmless.
STUCK_AFTER = timedelta(minutes=2)


async def execute(
    settings: Settings, org_id: uuid.UUID, investigation_id: uuid.UUID, owner: str
) -> RunOutcome:
    url = settings.database_url.get_secret_value()
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={
            "timeout": 5,
            "server_settings": {
                "application_name": "sentinelai-investigation",
                "statement_timeout": str(settings.db_statement_timeout_ms),
                "jit": "off",
            },
        },
    )
    try:
        sm = async_sessionmaker(engine, expire_on_commit=False)
        llm = build_client(settings, CommittingUsageSink(sm, SystemScope(org_id, "investigation")))
        async with open_checkpointer(url) as checkpointer:
            return await run_investigation(
                org_id=org_id,
                investigation_id=investigation_id,
                sessionmaker=sm,
                llm=llm,
                checkpointer=checkpointer,
                owner=owner,
            )
    finally:
        await engine.dispose()


async def dead_letter_run(
    settings: Settings,
    org_id: uuid.UUID,
    investigation_id: uuid.UUID,
    error_class: str,
    attempts: int,
) -> None:
    engine = create_async_engine(settings.database_url.get_secret_value(), poolclass=NullPool)
    try:
        await dead_letter(
            async_sessionmaker(engine, expire_on_commit=False),
            SystemScope(org_id, "investigation"),
            investigation_id,
            error_class,
            attempts,
        )
    finally:
        await engine.dispose()


async def find_stuck(settings: Settings, *, limit: int = 100) -> list[tuple[uuid.UUID, uuid.UUID]]:
    engine = create_async_engine(settings.database_url.get_secret_value(), poolclass=NullPool)
    try:
        async with async_sessionmaker(engine)() as s:
            return await stuck_investigations(s, stale=STUCK_AFTER, limit=limit)
    finally:
        await engine.dispose()
