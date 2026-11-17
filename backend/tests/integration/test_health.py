"""/health and /ready against real and deliberately unreachable dependencies."""

from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager
from sqlalchemy.ext.asyncio import AsyncEngine

from app.main import create_app
from tests.conftest import make_settings


async def test_ready_when_dependencies_up(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health")).status_code == 200
    resp = await client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["postgres"]["status"] == "ok"
    assert body["checks"]["redis"]["status"] == "ok"
    assert body["checks"]["postgres"]["latency_ms"] >= 0


@pytest.mark.parametrize(
    ("override", "down", "up"),
    [
        ({"redis_url": "redis://127.0.0.1:1/0"}, "redis", "postgres"),
        (
            {"database_url": "postgresql+asyncpg://sentinel:x@127.0.0.1:1/sentinel_test"},
            "postgres",
            "redis",
        ),
    ],
)
async def test_ready_503_with_details_when_dependency_down(
    engine: AsyncEngine, override: dict[str, str], down: str, up: str
) -> None:
    application = create_app(make_settings(**override))
    async with LifespanManager(application):
        transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            live = await c.get("/health")
            resp = await c.get("/ready")
    assert live.status_code == 200  # liveness does not depend on dependencies
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"][down]["status"] == "error"
    assert body["checks"][down]["error"]
    assert body["checks"][up]["status"] == "ok"
    assert "127.0.0.1:1" not in resp.text  # no connection details leak
