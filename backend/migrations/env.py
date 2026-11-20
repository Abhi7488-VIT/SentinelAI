"""Alembic environment (async, asyncpg)."""

from __future__ import annotations

import asyncio
import re
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401  (registers all models on Base.metadata)
from app.db.base import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Daily partitions (e.g. spans_p20261119) are created and dropped at runtime by the telemetry
# maintenance job, not by migrations; autogenerate must not report them as drift.
_PARTITION_RE = re.compile(r"^(?:spans|log_records|metric_points)_p\d{8}$")


def _include_name(name: str | None, type_: str, parent_names: object) -> bool:
    return not (type_ == "table" and name is not None and _PARTITION_RE.match(name))


def _database_url() -> str:
    url = config.attributes.get("database_url")
    if isinstance(url, str) and url:
        return url
    from app.core.config import get_settings

    return get_settings().database_url.get_secret_value()


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_name=_include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_name=_include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
