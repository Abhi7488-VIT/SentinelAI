"""Integration fixtures: real Postgres (migrated to head) and real Redis.

Data isolation between tests: every table is truncated after each test and the test Redis
database is flushed. Tests fail loudly (not skip) if the services are unreachable.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app import models as _models  # noqa: F401  (registers every table for TRUNCATE)
from app.core.config import Settings
from app.db.base import Base
from app.db.session import create_engine, create_sessionmaker
from app.main import create_app
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL, make_settings

BACKEND_DIR = Path(__file__).resolve().parents[2]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if BACKEND_DIR / "tests" / "integration" in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.integration)


def alembic_config(database_url: str = TEST_DATABASE_URL) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.attributes["database_url"] = database_url
    cfg.attributes["configure_logger"] = False
    return cfg


async def run_alembic(fn_name: str, *args: str) -> None:
    """Alembic's env.py calls asyncio.run(); run it in a thread with no running loop."""
    fn = getattr(command, fn_name)
    await asyncio.to_thread(fn, alembic_config(), *args)


@pytest.fixture(scope="session")
async def migrated_db() -> None:
    await run_alembic("downgrade", "base")
    await run_alembic("upgrade", "head")


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    return make_settings()


@pytest.fixture(scope="session")
async def engine(migrated_db: None, test_settings: Settings) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(test_settings, application_name="sentinelai-tests")
    yield eng
    await eng.dispose()


@pytest.fixture(scope="session")
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(engine)


@pytest.fixture
async def session(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as s:
        yield s


@pytest.fixture(scope="session")
async def redis_client() -> AsyncIterator[Redis]:
    client = Redis.from_url(TEST_REDIS_URL)
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
async def _clean_state(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    yield
    if "engine" in request.fixturenames:
        eng: AsyncEngine = request.getfixturevalue("engine")
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        async with eng.begin() as conn:
            await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    if {"engine", "redis_client"} & set(request.fixturenames):
        r = Redis.from_url(TEST_REDIS_URL)
        await r.flushdb()
        await r.aclose()


@pytest.fixture
async def app(engine: AsyncEngine, test_settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(test_settings)
    async with LifespanManager(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Origin": "http://localhost:3000"},
    ) as c:
        yield c
