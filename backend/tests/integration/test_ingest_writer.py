"""Ingest writer: rows land in the right tables, idempotently, with usage counted (Phase 3 T4)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.ingest.batch import NormalizedBatch
from app.ingest.dead_letter import DEAD_LETTER_KEY, dead_letter
from app.ingest.normalize import Window, normalize_logs, normalize_metrics, normalize_traces
from app.ingest.principal import IngestPrincipal
from app.services.ingest_writer import write_batch
from app.services.telemetry_maintenance import run_maintenance
from tests import otlp_factory as f
from tests.conftest import TEST_REDIS_URL, make_settings
from tests.integration.ingest_support import PB, TRACES, IngestFactory, make_ingest, sender

__all__ = ["make_ingest", "sender"]


@pytest.fixture
async def partitions(
    sessionmaker: async_sessionmaker[AsyncSession], test_settings: Settings
) -> None:
    async with sessionmaker() as s:
        await run_maintenance(s, test_settings)


def _principal(sender: dict[str, Any]) -> IngestPrincipal:
    return IngestPrincipal(
        key_id=uuid.UUID(sender["key"]["id"]),
        org_id=uuid.UUID(sender["actor"].org_id),
        service_id=uuid.UUID(sender["service"]["id"]),
        service_name=sender["service"]["name"],
    )


def _window() -> Window:
    return Window.around(f.now_ns() // 1000, max_age_s=7200, max_future_s=300)


async def _write(
    sm: async_sessionmaker[AsyncSession], batch: NormalizedBatch, settings: Settings
) -> Any:
    async with sm() as s:
        return await write_batch(s, batch, settings)


async def _count(sm: async_sessionmaker[AsyncSession], sql: str, **params: Any) -> Any:
    async with sm() as s:
        return (await s.execute(text(sql), params)).scalar()


async def _usage(sm: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    async with sm() as s:
        rows = await s.execute(
            text("SELECT outcome, sum(count) FROM ingest_usage GROUP BY outcome")
        )
        return {k: int(v) for k, v in rows.all()}


async def test_writes_every_signal_with_usage_and_bookkeeping(
    sender: dict[str, Any],
    partitions: None,
    sessionmaker: async_sessionmaker[AsyncSession],
    test_settings: Settings,
    client: httpx.AsyncClient,
) -> None:
    p = _principal(sender)
    trace = b"\x05" * 16
    traces = normalize_traces(
        f.traces_request(
            "order-service",
            [f.span(trace_id=trace), f.span(trace_id=trace, error=True), f.span(name="")],
        ),
        p,
        _window(),
    )
    logs = normalize_logs(
        f.logs_request("order-service", [f.log_record("a", trace_id=trace), f.log_record("b")]),
        p,
        _window(),
    )
    deploy = normalize_logs(
        f.logs_request(
            "deploy-pipeline",
            [
                f.log_record(
                    "deployment finished",
                    attributes={
                        "event.name": "deployment",
                        "deployment.id": "d1",
                        "deployment.service.name": "order-service",
                        "deployment.version": "1.2.1",
                        "deployment.status": "succeeded",
                    },
                )
            ],
        ),
        p,
        _window(),
    )
    metrics = normalize_metrics(
        f.metrics_request(
            "order-service",
            [
                f.gauge("db.client.connection.count", 10.0, attrs={"state": "used"}),
                f.counter("shop.orders.created", 5, start_ns=f.now_ns() - 10**9),
                f.histogram("http.server.request.duration", counts=[1, 2], bounds=[0.1]),
            ],
        ),
        p,
        _window(),
    )
    for batch in (traces, logs, deploy, metrics):
        result = await _write(sessionmaker, batch, test_settings)
        assert not result.skipped_duplicate_batch

    sm = sessionmaker
    assert await _count(sm, "SELECT count(*) FROM spans") == 2
    assert await _count(sm, "SELECT count(*) FROM spans WHERE status_code = 2") == 1
    assert await _count(sm, "SELECT count(*) FROM log_records") == 2
    assert await _count(sm, "SELECT count(*) FROM deployments WHERE version = '1.2.1'") == 1
    assert await _count(sm, "SELECT count(*) FROM metric_series") == 3
    assert await _count(sm, "SELECT count(*) FROM metric_points") == 3
    assert (
        await _count(sm, "SELECT temporality FROM metric_series WHERE name = 'shop.orders.created'")
        == 2
    )
    assert await _usage(sm) == {"accepted": 8, "rejected:missing_name": 1}

    # Service/key bookkeeping, visible through the API.
    svc = await client.get(
        f"/api/v1/orgs/{sender['actor'].org_id}/services/{sender['service']['id']}",
        headers=sender["actor"].headers,
    )
    assert svc.json()["last_seen_at"] is not None
    keys = await client.get(
        f"/api/v1/orgs/{sender['actor'].org_id}/services/{sender['service']['id']}/api-keys",
        headers=sender["actor"].headers,
    )
    assert keys.json()["items"][0]["last_used_at"] is not None


async def test_redelivered_batch_has_no_effect(
    sender: dict[str, Any],
    partitions: None,
    sessionmaker: async_sessionmaker[AsyncSession],
    test_settings: Settings,
) -> None:
    batch = normalize_traces(
        f.traces_request("order-service", [f.span(), f.span()]), _principal(sender), _window()
    )
    first = await _write(sessionmaker, batch, test_settings)
    second = await _write(sessionmaker, batch, test_settings)  # same batch_id: a redelivery
    assert first.inserted["spans"] == 2
    assert second.skipped_duplicate_batch
    assert await _count(sessionmaker, "SELECT count(*) FROM spans") == 2
    assert await _usage(sessionmaker) == {"accepted": 2}  # not double-counted


async def test_resent_records_in_a_new_batch_are_not_duplicated(
    sender: dict[str, Any],
    partitions: None,
    sessionmaker: async_sessionmaker[AsyncSession],
    test_settings: Settings,
) -> None:
    req = f.traces_request("order-service", [f.span() for _ in range(5)])
    for _ in range(2):  # a sender retrying the same export = two batches, same records
        batch = normalize_traces(req, _principal(sender), _window())
        await _write(sessionmaker, batch, test_settings)
    assert await _count(sessionmaker, "SELECT count(*) FROM spans") == 5


async def test_metric_cardinality_guard(
    sender: dict[str, Any], partitions: None, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    settings = make_settings(ingest_max_series_per_service=2)
    p = _principal(sender)
    gauges = [f.gauge("g", 1.0, attrs={"route": f"/r{i}"}) for i in range(3)]
    await _write(
        sessionmaker,
        normalize_metrics(f.metrics_request("order-service", gauges), p, _window()),
        settings,
    )
    assert await _count(sessionmaker, "SELECT count(*) FROM metric_series") == 2
    assert await _count(sessionmaker, "SELECT count(*) FROM metric_points") == 2
    assert await _usage(sessionmaker) == {"accepted": 2, "rejected:series_limit": 1}
    # known series keep flowing at the limit
    again = [f.gauge("g", 2.0, attrs={"route": "/r0"})]
    await _write(
        sessionmaker,
        normalize_metrics(f.metrics_request("order-service", again), p, _window()),
        settings,
    )
    assert await _count(sessionmaker, "SELECT count(*) FROM metric_points") == 3


async def test_rejection_only_batch_is_counted(
    sender: dict[str, Any],
    partitions: None,
    sessionmaker: async_sessionmaker[AsyncSession],
    test_settings: Settings,
) -> None:
    p = _principal(sender)
    batch = NormalizedBatch(
        org_id=p.org_id,
        service_id=p.service_id,
        key_id=p.key_id,
        signal="traces",
        received_us=f.now_ns() // 1000,
        rejected={"malformed_payload": 1},
    )
    await _write(sessionmaker, batch, test_settings)
    assert await _usage(sessionmaker) == {"rejected:malformed_payload": 1}


async def test_batch_cannot_attach_a_service_of_another_tenant(
    client: httpx.AsyncClient,
    sender: dict[str, Any],
    partitions: None,
    sessionmaker: async_sessionmaker[AsyncSession],
    test_settings: Settings,
) -> None:
    """Defense in depth: even a forged batch (org A, service of org B) is refused by the DB."""
    from tests.integration.factories import create_project, create_service, register

    bob = await register(client, "Bob")
    bob_project = await create_project(client, bob, bob.org_id, "B")
    bob_svc = await create_service(client, bob, bob.org_id, bob_project["id"], "order-service")
    forged = IngestPrincipal(
        key_id=uuid.UUID(sender["key"]["id"]),
        org_id=uuid.UUID(sender["actor"].org_id),
        service_id=uuid.UUID(bob_svc["id"]),
        service_name="order-service",
    )
    batch = normalize_traces(f.traces_request("order-service", [f.span()]), forged, _window())
    with pytest.raises(IntegrityError, match="fk_spans_service"):
        await _write(sessionmaker, batch, test_settings)


async def test_duplicate_otlp_request_end_to_end(
    make_ingest: IngestFactory,
    sender: dict[str, Any],
    partitions: None,
    sessionmaker: async_sessionmaker[AsyncSession],
    test_settings: Settings,
) -> None:
    """Acceptance: the same OTLP batch posted twice yields no duplicate rows."""
    from app.ingest.batch import decode_batch

    ingest, queue = await make_ingest()
    body = f.traces_request("order-service", [f.span() for _ in range(10)]).SerializeToString()
    for _ in range(2):
        resp = await ingest.post(TRACES, content=body, headers={**PB, **sender["auth"]})
        assert resp.status_code == 200
    malformed = await ingest.post(TRACES, content=b"\xff\xff\xff", headers={**PB, **sender["auth"]})
    assert malformed.status_code == 400
    assert len(queue.payloads) == 3
    for payload in queue.payloads:
        await _write(sessionmaker, decode_batch(payload), test_settings)
    assert await _count(sessionmaker, "SELECT count(*) FROM spans") == 10
    usage = await _usage(sessionmaker)
    assert usage == {"accepted": 20, "rejected:malformed_payload": 1}


async def test_dead_letter_list_is_capped(redis_client: Redis) -> None:
    from redis import Redis as SyncRedis

    settings = make_settings(ingest_dead_letter_max=3)
    sync = SyncRedis.from_url(TEST_REDIS_URL)
    try:
        for i in range(5):
            dead_letter(settings, f"payload-{i}", "OperationalError", redis=sync)
    finally:
        sync.close()
    entries = [json.loads(e) for e in await redis_client.lrange(DEAD_LETTER_KEY, 0, -1)]
    assert [e["payload"] for e in entries] == ["payload-4", "payload-3", "payload-2"]
    assert all(e["reason"] == "OperationalError" for e in entries)
