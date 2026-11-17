"""Telemetry reaches Prometheus, Jaeger and Loki for every shop service.

Requires the running testbed. Drives a little traffic first so the check is self-contained.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.integration

SHOP = os.environ.get("TESTBED_URL", "http://127.0.0.1:8081")
PROM = os.environ.get("TESTBED_PROMETHEUS_URL", "http://127.0.0.1:9090")
JAEGER = os.environ.get("TESTBED_JAEGER_URL", "http://127.0.0.1:16686")
LOKI = os.environ.get("TESTBED_LOKI_URL", "http://127.0.0.1:3100")

SERVICES = {"frontend", "api-gateway", "user-service", "order-service", "payment-service"}
DB_SERVICES = {"user-service", "order-service", "payment-service"}


async def _drive_traffic() -> None:
    async with httpx.AsyncClient(base_url=SHOP, timeout=15) as c:
        await c.post("/login", data={"email": "user11@shop.test", "password": "pw-11"})
        for _ in range(3):
            await c.get("/")
            await c.get("/products", params={"category": "garden"})
            await c.get("/account")
            await c.post("/checkout", data={"sku": "SKU-GAR-002", "qty": "1"})


async def _eventually(check: Any, timeout_s: float = 45.0) -> Any:
    deadline = time.monotonic() + timeout_s
    last: Any = None
    while time.monotonic() < deadline:
        last = await check()
        if last:
            return last
        await asyncio.sleep(3)
    return last


@pytest.fixture(scope="module")
async def traffic() -> None:
    await _drive_traffic()


async def _prom_services(query: str) -> set[str]:
    async with httpx.AsyncClient(timeout=10) as c:
        resp = await c.get(f"{PROM}/api/v1/query", params={"query": query})
    resp.raise_for_status()
    return {r["metric"].get("service_name", "") for r in resp.json()["data"]["result"]}


async def test_prometheus_has_http_and_pool_metrics(traffic: None) -> None:
    http = await _eventually(
        lambda: _prom_services(
            "count by (service_name) (http_server_request_duration_seconds_count)"
        )
    )
    assert http >= SERVICES
    pool = await _prom_services("max by (service_name) (db_client_connection_max)")
    assert pool >= DB_SERVICES
    memory = await _prom_services("max by (service_name) (container_memory_limit_bytes)")
    assert memory >= SERVICES


async def test_jaeger_has_every_service_and_full_chain_traces(traffic: None) -> None:
    async def services() -> set[str]:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.get(f"{JAEGER}/api/services")
        return set(resp.json().get("data") or [])

    assert await _eventually(services) >= SERVICES

    async with httpx.AsyncClient(timeout=15) as c:
        resp = await c.get(
            f"{JAEGER}/api/traces",
            params={
                "service": "frontend",
                "operation": "POST /checkout",
                "limit": 5,
                "lookback": "1h",
            },
        )
    traces = resp.json().get("data") or []
    assert traces, "no checkout traces"
    spanned = {
        trace["processes"][span["processID"]]["serviceName"]
        for trace in traces[:1]
        for span in trace["spans"]
    }
    assert spanned == SERVICES, spanned


async def test_loki_has_every_service_with_trace_ids(traffic: None) -> None:
    async def labels() -> set[str]:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.get(f"{LOKI}/loki/api/v1/label/service_name/values")
        return set(resp.json().get("data") or [])

    assert await _eventually(labels) >= SERVICES

    async def placed() -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.get(
                f"{LOKI}/loki/api/v1/query_range",
                params={
                    "query": '{service_name="order-service"} |= "order placed"',
                    "since": "15m",
                    "limit": 5,
                },
            )
        result: list[dict[str, Any]] = resp.json()["data"]["result"]
        return result

    streams = await _eventually(placed)
    assert streams, "no 'order placed' log lines"
    assert all(len(s["stream"].get("trace_id", "")) == 32 for s in streams)
