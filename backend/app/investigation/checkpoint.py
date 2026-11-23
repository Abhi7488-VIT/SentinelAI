"""LangGraph checkpoints in Postgres (plan §5, decision D5).

The library's tables live in their own `langgraph` schema (selected with `search_path` on the
checkpointer's psycopg connection), so they never mix with SentinelAI's Alembic-managed tables and
`alembic check` ignores them. `setup_checkpointer()` creates the schema and runs the library's own
migrations; it is idempotent and runs from the one-shot `migrate` service after Alembic.

Checkpoint rows carry no `org_id` (documented exception to invariant 4, ADR 0007): they are keyed
by `thread_id` = investigation id, never exposed through the API, and read only by the worker
after it has loaded the org-scoped investigation row. State holds ids and summaries only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url

CHECKPOINT_SCHEMA = "langgraph"


def psycopg_conninfo(database_url: str) -> str:
    """SQLAlchemy URL (any driver) -> libpq URL for psycopg."""
    url = make_url(database_url).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


async def _connect(database_url: str) -> AsyncConnection[dict[str, object]]:
    return await AsyncConnection.connect(
        psycopg_conninfo(database_url),
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
        options=f"-c search_path={CHECKPOINT_SCHEMA}",
        application_name="sentinelai-checkpointer",
    )


@asynccontextmanager
async def open_checkpointer(database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    async with await _connect(database_url) as conn:
        yield AsyncPostgresSaver(conn=conn)


async def setup_checkpointer(database_url: str) -> None:
    async with await _connect(database_url) as conn:
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {CHECKPOINT_SCHEMA}")
        await AsyncPostgresSaver(conn=conn).setup()
