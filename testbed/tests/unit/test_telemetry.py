"""Telemetry contract, checked in-process with in-memory exporters (no Collector needed)."""

from __future__ import annotations

import io
import json
import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from shop.common.app import create_service_app
from shop.common.db import Database
from shop.common.errors import ServiceUnavailableError
from shop.common.settings import ServiceSettings
from shop.common.telemetry import Telemetry

SERVICES_DIR = Path(__file__).resolve().parents[2] / "services"


class Harness:
    def __init__(self) -> None:
        self.spans = InMemorySpanExporter()
        self.metrics = InMemoryMetricReader()
        self.logs = InMemoryLogRecordExporter()  # type: ignore[no-untyped-call]
        settings = ServiceSettings(
            service_name="order-service",
            service_version="1.7.2",
            database_url="postgresql+asyncpg://u:p@127.0.0.1:1/orders",  # never connected
        )
        self.telemetry = Telemetry(
            settings, span_exporter=self.spans, metric_reader=self.metrics, log_exporter=self.logs
        )
        self.app = create_service_app(settings, telemetry=self.telemetry)
        self.db = Database(settings)
        self.db.instrument(self.telemetry)

        @self.app.get("/boom")
        async def boom() -> None:
            logging.getLogger("shop.test").warning("about to fail")
            raise ServiceUnavailableError("Timed out waiting for a database connection.")

        @self.app.get("/ok")
        async def ok() -> dict[str, str]:
            logging.getLogger("shop.test").info("all good")
            return {"ok": "yes"}

    def flush(self) -> None:
        assert self.telemetry.tracer_provider is not None
        assert self.telemetry.logger_provider is not None
        self.telemetry.tracer_provider.force_flush()
        self.telemetry.logger_provider.force_flush()

    def metric_names(self) -> set[str]:
        data = self.metrics.get_metrics_data()
        assert data is not None
        return {
            m.name for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics
        }


@pytest.fixture(scope="module")
def harness() -> Harness:
    return Harness()


@pytest.fixture
async def client(harness: Harness) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=harness.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


def _log_trace_id(record: Any) -> int:
    inner = getattr(record, "log_record", record)
    return int(inner.trace_id)


async def test_resource_attributes(harness: Harness) -> None:
    attrs = dict(harness.telemetry.resource.attributes)
    assert attrs["service.name"] == "order-service"
    assert attrs["service.namespace"] == "shop"
    assert attrs["service.version"] == "1.7.2"
    assert attrs["deployment.environment.name"] == "testbed"
    assert re.fullmatch(r"[0-9a-f]{32}", str(attrs["service.instance.id"]))


async def test_error_span_and_correlated_logs(harness: Harness, client: httpx.AsyncClient) -> None:
    harness.spans.clear()
    stdout_handler = logging.getLogger().handlers[0]
    assert isinstance(stdout_handler, logging.StreamHandler)
    buffer = io.StringIO()
    previous = stdout_handler.setStream(buffer)
    try:
        resp = await client.get("/boom")
    finally:
        stdout_handler.setStream(previous)
    assert resp.status_code == 503
    harness.flush()

    server = [s for s in harness.spans.get_finished_spans() if s.kind.name == "SERVER"]
    assert len(server) == 1
    span = server[0]
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes is not None
    assert span.attributes.get("http.response.status_code") == 503  # stable HTTP semconv
    assert span.attributes.get("http.request.method") == "GET"
    assert any(e.name == "exception" for e in span.events)

    # OTLP log records carry the same trace id as the request's span.
    otlp_ids = {_log_trace_id(r) for r in harness.logs.get_finished_logs()}
    assert span.context.trace_id in otlp_ids

    # ...and so do the JSON lines on stdout.
    lines = [json.loads(ln) for ln in buffer.getvalue().splitlines() if ln.startswith("{")]
    failing = [ln for ln in lines if ln["msg"] == "about to fail"]
    assert failing
    assert failing[-1]["trace_id"] == format(span.context.trace_id, "032x")


async def test_healthz_is_not_traced(harness: Harness, client: httpx.AsyncClient) -> None:
    harness.spans.clear()
    assert (await client.get("/healthz")).status_code == 200
    harness.flush()
    assert harness.spans.get_finished_spans() == ()


async def test_metric_names(harness: Harness, client: httpx.AsyncClient) -> None:
    await client.get("/ok")
    names = harness.metric_names()
    expected = {
        "http.server.request.duration",
        "db.client.connection.count",
        "db.client.connection.max",
        "db.client.connection.pending_requests",
        "db.client.connection.timeouts",
        "process.cpu.time",
    }
    assert expected <= names, expected - names


def test_no_tell_tale_words_in_the_system_under_test() -> None:
    """ADR 0003: nothing in the observed system names faults or their tooling."""
    banned = re.compile(
        r"\b(chaos|faults?|inject\w*|scenarios?|ground.?truth|toxiproxy)\b", re.IGNORECASE
    )
    offenders = [
        f"{path.relative_to(SERVICES_DIR)}:{n}: {line.strip()}"
        for path in SERVICES_DIR.rglob("*")
        if path.suffix in {".py", ".html"}
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if banned.search(line)
    ]
    assert not offenders, offenders
