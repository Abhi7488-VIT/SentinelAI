"""Shared fixtures for ingest tests: a recording queue, an ingest-app factory, a keyed sender."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from app.ingest.batch import NormalizedBatch, decode_batch
from app.ingest.queue import IngestUnavailableError
from app.ingest_main import create_ingest_app
from tests.conftest import make_settings
from tests.integration.factories import (
    create_api_key,
    create_project,
    create_service,
    register,
)

PB = {"Content-Type": "application/x-protobuf"}
TRACES = "/api/v1/otlp/v1/traces"
LOGS = "/api/v1/otlp/v1/logs"
METRICS = "/api/v1/otlp/v1/metrics"


class RecordingQueue:
    def __init__(self) -> None:
        self.payloads: list[str] = []
        self.full = False

    async def pending(self) -> int:
        return len(self.payloads)

    async def enqueue(self, payload: str) -> None:
        if self.full:
            raise IngestUnavailableError("The ingest queue is full.", code="ingest_queue_full")
        self.payloads.append(payload)

    @property
    def batches(self) -> list[NormalizedBatch]:
        return [decode_batch(p) for p in self.payloads]


IngestFactory = Callable[..., Awaitable[tuple[httpx.AsyncClient, RecordingQueue]]]


@pytest.fixture
async def make_ingest(engine: Any) -> AsyncIterator[IngestFactory]:
    stack: list[tuple[LifespanManager, httpx.AsyncClient]] = []

    async def factory(**overrides: Any) -> tuple[httpx.AsyncClient, RecordingQueue]:
        settings = make_settings(ingest_key_cache_seconds=0, **overrides)
        queue = RecordingQueue()
        app: FastAPI = create_ingest_app(settings, queue=queue)
        manager = LifespanManager(app)
        await manager.__aenter__()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://ingest",
        )
        stack.append((manager, client))
        return client, queue

    yield factory
    for manager, client in stack:
        await client.aclose()
        await manager.__aexit__(None, None, None)


@pytest.fixture
async def sender(client: httpx.AsyncClient) -> dict[str, Any]:
    """A registered service with an ingest key (created through the real API)."""
    alice = await register(client, "Alice")
    project = await create_project(client, alice, alice.org_id, "Shop")
    service = await create_service(client, alice, alice.org_id, project["id"], "order-service")
    key = await create_api_key(client, alice, alice.org_id, service["id"])
    return {
        "actor": alice,
        "project": project,
        "service": service,
        "key": key,
        "auth": {"Authorization": f"Bearer {key['key']}"},
    }
