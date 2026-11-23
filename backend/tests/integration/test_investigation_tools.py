"""Evidence store and deterministic tools (Phase 4 T2) on Postgres with seeded telemetry.

The fixture is a generic three-hop shape (gateway -> orders -> a database), built for these
tests; it is not derived from any testbed scenario (ADR 0003).
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.config import Settings
from app.core.tenancy import SystemScope
from app.ingest.batch import DeploymentRow, LogRow, SpanRow
from app.investigation.tools import (
    ToolContext,
    ToolInputError,
    dependency_lookup,
    deploy_lookup,
    detection_ref,
    log_query,
    metric_query,
    trace_analysis,
)
from app.models.investigation import EVIDENCE_DATA_MAX_BYTES
from app.models.telemetry import SpanKind, SpanStatus
from app.repositories.investigation_queries import (
    LogEvidenceQueries,
    MetricEvidenceQueries,
    TraceEvidenceQueries,
    Window,
)
from app.repositories.investigations import EvidenceRepository
from app.repositories.telemetry import DeploymentRepository, LogRecordRepository, SpanRepository
from app.services.telemetry_maintenance import run_maintenance
from tests.integration.factories import create_project, create_service, register
from tests.integration.telemetry_support import SM, rows, sql, us

S = timedelta(seconds=1)
M = timedelta(minutes=1)
POOL_MSG = "connection pool exhausted after 30000ms waiting for a connection"
STACK = (
    "Traceback (most recent call last):\n"
    '  File "/app/handlers.py", line 20, in create_order\n'
    '  File "/app/storage.py", line 77, in acquire\n'
    "TimeoutError: connection pool exhausted"
)


@pytest.fixture
async def partitions(sessionmaker: SM, test_settings: Settings) -> None:
    async with sessionmaker() as s:
        await run_maintenance(s, test_settings)


@pytest.fixture
def t0() -> datetime:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    return now - timedelta(minutes=30)


def _span(
    *,
    trace: bytes,
    parent: bytes | None,
    at: datetime,
    ms: float,
    kind: SpanKind,
    name: str,
    error: bool = False,
    db: bool = False,
) -> tuple[bytes, SpanRow]:
    sid = os.urandom(8)
    return sid, SpanRow(
        trace_id=trace,
        span_id=sid,
        parent_span_id=parent,
        name=name,
        kind=int(kind),
        start_us=us(at),
        end_us=us(at + timedelta(milliseconds=ms)),
        duration_ms=ms,
        status_code=int(SpanStatus.ERROR if error else SpanStatus.UNSET),
        status_message=None,
        http_method=None if db else "POST",
        http_route=None if db else "/orders",
        http_status_code=None if db else (500 if error else 200),
        db_system="postgresql" if db else None,
        server_address="orders-db" if db else None,
        peer_service=None,
        exception_type="TimeoutError" if error else None,
        exception_message=f"connection pool exhausted after {int(ms)}ms" if error else None,
        service_instance_id="i-1",
        service_version="2.0.0",
        attributes={"db.namespace": "orders"} if db else {},
    )


async def _insert(sm: SM, org: str, svc: str, spans: list[SpanRow]) -> None:
    async with sm() as s:
        await SpanRepository(s, SystemScope(uuid.UUID(org), "test")).insert_many(
            uuid.UUID(svc), spans
        )
        await s.commit()


async def _logs(sm: SM, org: str, svc: str, logs: list[LogRow]) -> None:
    async with sm() as s:
        await LogRecordRepository(s, SystemScope(uuid.UUID(org), "test")).insert_many(
            uuid.UUID(svc), logs
        )
        await s.commit()


def _log(at: datetime, body: str, *, sev: int = 17, exc: bool = False) -> LogRow:
    return LogRow(
        record_hash=hashlib.sha256(
            f"{at.isoformat()}{body}{os.urandom(4).hex()}".encode()
        ).digest(),
        ts_us=us(at),
        observed_us=None,
        severity_number=sev,
        severity_text={17: "ERROR", 13: "WARN", 9: "INFO"}[sev],
        body=body,
        trace_id=None,
        span_id=None,
        event_name=None,
        exception_type="TimeoutError" if exc else None,
        exception_message=body if exc else None,
        exception_stacktrace=STACK if exc else None,
        service_instance_id="i-1",
        service_version="2.0.0",
        attributes={"pool.name": "orders"},
    )


@pytest.fixture
async def world(
    client: httpx.AsyncClient, sessionmaker: SM, partitions: None, t0: datetime
) -> dict[str, Any]:
    """Org with gateway -> orders -> postgresql:orders; healthy baseline [t0-10m, t0), failing
    incident window [t0, t0+10m) where orders' pool times out from t0+1m."""
    owner = await register(client, "Owner", "Tools Org")
    org = owner.org_id
    project = await create_project(client, owner, org, "Shop")
    gw = (await create_service(client, owner, org, project["id"], "gateway"))["id"]
    orders = (await create_service(client, owner, org, project["id"], "orders"))["id"]
    resp = await client.put(
        f"/api/v1/orgs/{org}/services/{gw}/dependencies",
        json={"dependencies": [{"target_service_id": orders}]},
        headers=owner.headers,
    )
    assert resp.status_code == 200, resp.text

    gw_spans: list[SpanRow] = []
    order_spans: list[SpanRow] = []
    t = t0 - 10 * M
    while t < t0 + 10 * M:
        failing = t >= t0 + M
        trace = os.urandom(16)
        root_ms, svc_ms, db_ms = (900.0, 850.0, 800.0) if failing else (30.0, 20.0, 5.0)
        root, row = _span(
            trace=trace,
            parent=None,
            at=t,
            ms=root_ms,
            kind=SpanKind.SERVER,
            name="POST /orders",
            error=failing,
        )
        gw_spans.append(row)
        call, row = _span(
            trace=trace,
            parent=root,
            at=t + 2 * timedelta(milliseconds=1),
            ms=svc_ms + 10,
            kind=SpanKind.CLIENT,
            name="POST orders",
        )
        gw_spans.append(row)
        srv, row = _span(
            trace=trace,
            parent=call,
            at=t + 5 * timedelta(milliseconds=1),
            ms=svc_ms,
            kind=SpanKind.SERVER,
            name="POST /orders",
            error=failing,
        )
        order_spans.append(row)
        _, row = _span(
            trace=trace,
            parent=srv,
            at=t + 10 * timedelta(milliseconds=1),
            ms=db_ms,
            kind=SpanKind.CLIENT,
            name="SELECT orders",
            error=failing,
            db=True,
        )
        order_spans.append(row)
        t += 5 * S
    await _insert(sessionmaker, org, gw, gw_spans)
    await _insert(sessionmaker, org, orders, order_spans)

    logs = [_log(t0 + M + i * 10 * S, f"{POOL_MSG} (attempt {i})", exc=True) for i in range(30)]
    logs += [_log(t0 + 2 * M + i * S, f"slow query took {100 + i}ms", sev=13) for i in range(5)]
    logs += [_log(t0 - 5 * M, "baseline note", sev=9)]
    await _logs(sessionmaker, org, orders, logs)

    digest = hashlib.sha256(json.dumps([orders, "pool.used"]).encode()).digest()
    (series,) = await rows(
        sessionmaker,
        "INSERT INTO metric_series (org_id, service_id, series_hash, name, kind, is_monotonic,"
        " temporality, attributes) VALUES (:o, :s, :h, 'db.client.connection.count', 2, false, 2,"
        " CAST(:a AS jsonb)) RETURNING id",
        o=org,
        s=orders,
        h=digest,
        a=json.dumps({"db.client.connection.state": "used", "pool.name": "orders"}),
    )
    t = t0 - 10 * M
    while t < t0 + 10 * M:
        await sql(
            sessionmaker,
            "INSERT INTO metric_points (org_id, series_id, ts, value) VALUES (:o, :s, :t, :v)",
            o=org,
            s=series["id"],
            t=t,
            v=10.0 if t >= t0 + M else 2.0 + (t.minute % 2) * 0.5,
        )
        t += 15 * S

    async with sessionmaker() as s:
        await DeploymentRepository(s, SystemScope(uuid.UUID(org), "test")).insert_many(
            uuid.UUID(orders),
            [DeploymentRow("dep-42", "started", "2.0.0", "1.9.3", us(t0 - 5 * M))],
        )
        await s.commit()
    await sql(
        sessionmaker,
        "INSERT INTO service_edges (org_id, source_service_id, target_external, source,"
        " first_seen_at, last_seen_at, call_count, sample_trace_id)"
        " VALUES (:o, :s, 'postgresql:orders', 'observed', :f, :l, 10, :tr)",
        o=org,
        s=orders,
        f=t0 - 10 * M,
        l=t0 + 10 * M,
        tr=os.urandom(16),
    )

    (inc,) = await rows(
        sessionmaker,
        "INSERT INTO incidents (org_id, project_id, number, title, status, severity, started_at,"
        " detected_at, last_activity_at) VALUES (:o, :p, 1, 'Errors on gateway, orders', 'open',"
        " 'critical', :s, :d, :d) RETURNING id",
        o=org,
        p=project["id"],
        s=t0 + M,
        d=t0 + 3 * M,
    )
    (det,) = await rows(
        sessionmaker,
        "INSERT INTO detections (org_id, service_id, rule, severity, metric, observed_value,"
        " baseline_value, threshold, started_at, fired_at, explanation) VALUES (:o, :s,"
        " 'error_rate', 'critical', 'error_rate', 0.98, 0.0, 0.05, :st, :f,"
        " 'error rate 98% over 60 s') RETURNING id",
        o=org,
        s=orders,
        st=t0 + M,
        f=t0 + 2 * M,
    )
    await sql(
        sessionmaker,
        "INSERT INTO incident_detections (org_id, incident_id, detection_id, reasons)"
        " VALUES (:o, :i, :d, CAST(:r AS jsonb))",
        o=org,
        i=inc["id"],
        d=det["id"],
        r=json.dumps([{"type": "opener", "detail": "first detection"}]),
    )
    (inv,) = await rows(
        sessionmaker,
        "INSERT INTO investigations (org_id, incident_id, status) VALUES (:o, :i, 'running')"
        " RETURNING id",
        o=org,
        i=inc["id"],
    )
    return {
        "owner": owner,
        "org": uuid.UUID(org),
        "gw": uuid.UUID(gw),
        "orders": uuid.UUID(orders),
        "incident": inc["id"],
        "inv": inv["id"],
        "t0": t0,
    }


async def _ctx(sm: SM, w: dict[str, Any]) -> tuple[Any, ToolContext]:
    session = sm()
    scope = SystemScope(w["org"], "investigation")
    return session, await ToolContext.open(session, scope, w["inv"], "test")


async def test_log_query_clusters_errors_and_counts_everything(
    sessionmaker: SM, world: dict[str, Any]
) -> None:
    t0 = world["t0"]
    session, ctx = await _ctx(sessionmaker, world)
    async with session:
        ev = await log_query(ctx, service_id=world["orders"], start=t0, end=t0 + 10 * M)
        await session.commit()
    assert ev.kind == "log_query"
    assert ev.id.startswith("ev_")
    assert ev.row_count == 35
    assert not ev.truncated
    top, second = ev.data["clusters"][:2]
    assert (
        top["template"]
        == "TimeoutError: connection pool exhausted after <n>ms waiting for a connection"
        " (attempt <n>)"
    )
    assert top["count"] == 30
    assert len(top["example_ids"]) == 3
    assert top["top_attributes"][0] == {"key": "pool.name", "value": "orders", "count": 30}
    assert second == {**second, "template": "slow query took <n>ms", "count": 5}
    assert ev.summary.startswith("35 log records at severity >= 13 for orders in [")
    assert ev.source_ref["tool"] == "log_query"
    assert ev.source_ref["params"]["service_id"] == str(world["orders"])
    assert ev.service_ids == [str(world["orders"])]


async def test_rerun_returns_existing_evidence_and_new_params_create_new(
    sessionmaker: SM, world: dict[str, Any]
) -> None:
    t0 = world["t0"]
    ids = []
    for _ in range(2):
        session, ctx = await _ctx(sessionmaker, world)
        async with session:
            ids.append(
                (await log_query(ctx, service_id=world["orders"], start=t0, end=t0 + 10 * M)).id
            )
            await session.commit()
    assert ids[0] == ids[1]
    session, ctx = await _ctx(sessionmaker, world)
    async with session:
        other = await log_query(
            ctx, service_id=world["orders"], start=t0, end=t0 + 10 * M, min_severity=17
        )
        await session.commit()
    assert other.id != ids[0]
    assert other.row_count == 30
    (n,) = await rows(
        sessionmaker,
        "SELECT count(*) AS n FROM evidence WHERE investigation_id = :i",
        i=world["inv"],
    )
    assert n["n"] == 2


async def test_empty_window_is_explicit_evidence(sessionmaker: SM, world: dict[str, Any]) -> None:
    t0 = world["t0"]
    session, ctx = await _ctx(sessionmaker, world)
    async with session:
        ev = await log_query(ctx, service_id=world["gw"], start=t0, end=t0 + 10 * M)
        tr = await trace_analysis(
            ctx, service_ids=[world["gw"]], start=t0 - 4 * 60 * M, end=t0 - 3 * 60 * M
        )
        await session.commit()
    assert ev.row_count == 0
    assert ev.data["clusters"] == []
    assert ev.summary.startswith("No log records at severity >= 13 for gateway in [")
    assert tr.summary.startswith("No spans for gateway in baseline [")
    assert tr.data["critical_paths"] == []


async def test_metric_query_reports_delta_and_onset(
    sessionmaker: SM, world: dict[str, Any]
) -> None:
    t0 = world["t0"]
    session, ctx = await _ctx(sessionmaker, world)
    async with session:
        ev = await metric_query(
            ctx,
            service_id=world["orders"],
            start=t0,
            end=t0 + 10 * M,
            baseline_start=t0 - 10 * M,
            baseline_end=t0,
        )
        await session.commit()
    (series,) = ev.data["series"]
    assert series["name"] == "db.client.connection.count"
    assert series["measure"] == "value"
    assert series["baseline"]["max"] == 2.5
    assert series["incident"]["max"] == 10.0
    assert series["change_pct"] is not None
    assert series["change_pct"] > 200
    assert series["onset"] == (t0 + M).isoformat()
    assert "db.client.connection.count" in ev.summary
    assert "onset" in ev.summary
    assert ev.time_from == t0 - 10 * M
    assert ev.time_to == t0 + 10 * M


async def test_trace_analysis_operations_signatures_and_critical_path(
    sessionmaker: SM, world: dict[str, Any]
) -> None:
    t0 = world["t0"]
    session, ctx = await _ctx(sessionmaker, world)
    async with session:
        ev = await trace_analysis(
            ctx, service_ids=[world["gw"], world["orders"]], start=t0, end=t0 + 10 * M
        )
        await session.commit()
    ops = {(o["service"], o["operation"], o["kind"]): o for o in ev.data["operations"]}
    db = ops[("orders", "SELECT orders", "client")]
    assert db["baseline"]["error_pct"] == 0.0
    assert db["incident"]["error_pct"] == pytest.approx(90.0)  # failing from t0 + 1 m
    assert db["p95_change_pct"] is not None
    assert db["p95_change_pct"] > 1000
    sig = ev.data["exception_signatures"][0]
    assert sig["type"] == "TimeoutError"
    assert sig["message"] == "connection pool exhausted after <n>ms"
    assert sig["span_count"] > 0
    log_sig = next(s for s in ev.data["exception_signatures"] if s["log_count"])
    assert log_sig["top_frame"] == "/app/storage.py:77 in acquire"
    slow = next(p for p in ev.data["critical_paths"] if p["reason"] == "slowest")
    assert [(e["service"], e["operation"]) for e in slow["path"]] == [
        ("gateway", "POST /orders"),
        ("gateway", "POST orders"),
        ("orders", "POST /orders"),
        ("orders", "SELECT orders"),
    ]
    assert slow["path"][-1]["self_ms"] == 800.0
    assert any(p["reason"] == "erroring" for p in ev.data["critical_paths"])
    assert "Slowest critical path" in ev.summary


async def test_trace_operations_are_inbound_and_database_spans(
    sessionmaker: SM, world: dict[str, Any]
) -> None:
    t0 = world["t0"]
    session, ctx = await _ctx(sessionmaker, world)
    async with session:
        ev = await trace_analysis(ctx, service_ids=[world["gw"]], start=t0, end=t0 + 10 * M)
    # gateway's non-database CLIENT span is covered by dependency_lookup, not here.
    assert [(o["operation"], o["kind"]) for o in ev.data["operations"]] == [
        ("POST /orders", "server")
    ]


async def test_deploy_lookup_reports_distance_to_onset(
    sessionmaker: SM, world: dict[str, Any]
) -> None:
    t0 = world["t0"]
    session, ctx = await _ctx(sessionmaker, world)
    async with session:
        ev = await deploy_lookup(
            ctx, service_ids=[world["orders"], world["gw"]], start=t0, end=t0 + 10 * M, onset=t0 + M
        )
        none = await deploy_lookup(ctx, service_ids=[world["gw"]], start=t0, end=t0 + 10 * M)
    (dep,) = ev.data["deployments"]
    assert dep == {**dep, "service": "orders", "version": "2.0.0", "previous_version": "1.9.3"}
    assert dep["minutes_before_onset"] == 6.0
    assert "orders 1.9.3 -> 2.0.0" in ev.summary
    assert ev.time_from == t0 - 2 * 60 * M
    assert none.summary.startswith("No deployments of gateway in [")


async def test_dependency_lookup_edges_with_traffic_change(
    sessionmaker: SM, world: dict[str, Any]
) -> None:
    t0 = world["t0"]
    session, ctx = await _ctx(sessionmaker, world)
    async with session:
        ev = await dependency_lookup(ctx, service_ids=[world["orders"]], start=t0, end=t0 + 10 * M)
    edges = {(e["source"], e["target"]): e for e in ev.data["edges"]}
    svc_edge = edges[("gateway", "orders")]
    assert svc_edge["sources"] == ["declared"]
    assert svc_edge["target_kind"] == "service"
    # gateway's CLIENT span is the parent of orders' SERVER span.
    assert svc_edge["stats"]["baseline"]["calls"] == 120
    assert svc_edge["stats"]["incident"]["error_pct"] == pytest.approx(90.0)
    db_edge = edges[("orders", "postgresql:orders")]
    assert db_edge["target_kind"] == "external"
    assert db_edge["sources"] == ["observed"]
    assert db_edge["stats"]["incident"]["errors"] == 108
    assert ev.summary.startswith("2 dependency edges around orders")


async def test_detection_ref_lists_detections_and_reasons(
    sessionmaker: SM, world: dict[str, Any]
) -> None:
    session, ctx = await _ctx(sessionmaker, world)
    async with session:
        ev = await detection_ref(ctx, incident_id=world["incident"])
    (det,) = ev.data["detections"]
    assert det["service"] == "orders"
    assert det["rule"] == "error_rate"
    assert det["grouping_reasons"] == [{"type": "opener", "detail": "first detection"}]
    assert ev.data["incident"]["number"] == 1
    assert ev.summary.startswith("Incident #1 (critical, open): 1 detections on orders.")
    assert ev.time_from == world["t0"] + M


async def test_data_is_capped_and_marked_truncated(sessionmaker: SM, world: dict[str, Any]) -> None:
    t0 = world["t0"]
    # 400 distinct ~280-char templates: far over 16 KiB before trimming.
    words = [
        hashlib.sha256(str(i).encode()).hexdigest()[:8].replace("0", "g").replace("1", "h")
        for i in range(400)
    ]
    noisy = [
        _log(
            t0 + 5 * M + i * 100 * timedelta(milliseconds=1),
            f"{''.join(c for c in w if c.isalpha())}-failure " + ("context " * 33),
        )
        for i, w in enumerate(words)
    ]
    await _logs(sessionmaker, str(world["org"]), str(world["gw"]), noisy)
    session, ctx = await _ctx(sessionmaker, world)
    async with session:
        ev = await log_query(ctx, service_id=world["gw"], start=t0, end=t0 + 10 * M)
        await session.commit()
    assert ev.truncated
    (size,) = await rows(
        sessionmaker, "SELECT octet_length(data::text) AS n FROM evidence WHERE id = :i", i=ev.id
    )
    assert 0 < size["n"] <= EVIDENCE_DATA_MAX_BYTES
    assert len(ev.summary) <= 1000

    with pytest.raises(IntegrityError, match="ck_evidence_data_size"):
        await sql(
            sessionmaker,
            "INSERT INTO evidence (id, org_id, investigation_id, kind, source_ref, time_from,"
            " time_to,"
            " summary, data, row_count, truncated, dedup_key, created_by_node) VALUES"
            " ('ev_aaaaaaaaaaaa', :o, :i, 'log_query', '{}', :f, :t, 's', CAST(:d AS jsonb), 0,"
            " false, :k, 'x')",
            o=world["org"],
            i=world["inv"],
            f=t0,
            t=t0 + M,
            d=json.dumps({"blob": "x" * 20_000}),
            k="0" * 64,
        )


async def test_tools_validate_windows(sessionmaker: SM, world: dict[str, Any]) -> None:
    t0 = world["t0"]
    session, ctx = await _ctx(sessionmaker, world)
    async with session:
        with pytest.raises(ToolInputError, match="6 hours"):
            await log_query(ctx, service_id=world["gw"], start=t0 - 7 * 60 * M, end=t0)
        with pytest.raises(ToolInputError, match="non-empty"):
            await log_query(ctx, service_id=world["gw"], start=t0, end=t0)
        with pytest.raises(ToolInputError, match="timezone"):
            await log_query(ctx, service_id=world["gw"], start=t0.replace(tzinfo=None), end=t0 + M)
        with pytest.raises(ToolInputError, match="baseline must end"):
            await metric_query(
                ctx,
                service_id=world["gw"],
                start=t0,
                end=t0 + M,
                baseline_start=t0 - M,
                baseline_end=t0 + S,
            )
        with pytest.raises(ToolInputError, match="min_severity"):
            await log_query(ctx, service_id=world["gw"], start=t0, end=t0 + M, min_severity=99)


async def test_cross_org_ids_are_rejected(
    client: httpx.AsyncClient, sessionmaker: SM, world: dict[str, Any]
) -> None:
    t0 = world["t0"]
    intruder = await register(client, "Intruder", "Other Org")
    other = SystemScope(uuid.UUID(intruder.org_id), "investigation")
    # Another org cannot open this investigation ...
    async with sessionmaker() as s:
        with pytest.raises(ToolInputError, match="investigation not found"):
            await ToolContext.open(s, other, world["inv"], "test")
    # ... nor read this org's evidence ...
    session, ctx = await _ctx(sessionmaker, world)
    async with session:
        ev = await log_query(ctx, service_id=world["orders"], start=t0, end=t0 + 10 * M)
        await session.commit()
    async with sessionmaker() as s:
        assert await EvidenceRepository(s, other).get_in_investigation(world["inv"], ev.id) is None
        assert await EvidenceRepository(s, other).existing_ids(world["inv"], [ev.id]) == set()
    # ... and a tool of this org refuses a foreign service id or incident.
    project = await create_project(client, intruder, intruder.org_id, "Theirs")
    foreign = uuid.UUID(
        (await create_service(client, intruder, intruder.org_id, project["id"], "x"))["id"]
    )
    session, ctx = await _ctx(sessionmaker, world)
    async with session:
        with pytest.raises(ToolInputError, match="unknown service"):
            await log_query(ctx, service_id=foreign, start=t0, end=t0 + M)
        with pytest.raises(ToolInputError, match="unknown service"):
            await trace_analysis(ctx, service_ids=[world["gw"], foreign], start=t0, end=t0 + M)
        with pytest.raises(ToolInputError, match="incident not found"):
            await detection_ref(ctx, incident_id=uuid.uuid4())


async def test_evidence_requires_a_same_org_investigation(
    client: httpx.AsyncClient, sessionmaker: SM, world: dict[str, Any]
) -> None:
    intruder = await register(client, "Intruder", "Other Org")
    with pytest.raises(IntegrityError, match="fk_evidence_investigation"):
        await sql(
            sessionmaker,
            "INSERT INTO evidence (id, org_id, investigation_id, kind, source_ref, time_from,"
            " time_to,"
            " summary, data, row_count, truncated, dedup_key, created_by_node) VALUES"
            " ('ev_bbbbbbbbbbbb', :o, :i, 'log_query', '{}', now() - interval '1 minute', now(),"
            " 's', '{}', 0, false, :k, 'x')",
            o=intruder.org_id,
            i=world["inv"],
            k="1" * 64,
        )
    with pytest.raises(DBAPIError, match="ck_evidence_id_format"):
        await sql(
            sessionmaker,
            "INSERT INTO evidence (id, org_id, investigation_id, kind, source_ref, time_from,"
            " time_to,"
            " summary, data, row_count, truncated, dedup_key, created_by_node) VALUES"
            " ('made-up-id', :o, :i, 'log_query', '{}', now() - interval '1 minute', now(),"
            " 's', '{}', 0, false, :k, 'x')",
            o=world["org"],
            i=world["inv"],
            k="2" * 64,
        )


async def test_one_active_investigation_per_incident(
    sessionmaker: SM, world: dict[str, Any]
) -> None:
    with pytest.raises(IntegrityError, match="uq_investigations_active_incident"):
        await sql(
            sessionmaker,
            "INSERT INTO investigations (org_id, incident_id, status) VALUES (:o, :i, 'queued')",
            o=world["org"],
            i=world["incident"],
        )
    await sql(
        sessionmaker, "UPDATE investigations SET status = 'completed' WHERE id = :i", i=world["inv"]
    )
    await sql(
        sessionmaker,
        "INSERT INTO investigations (org_id, incident_id, status) VALUES (:o, :i, 'queued')",
        o=world["org"],
        i=world["incident"],
    )


def _plan_nodes(plan: Any) -> list[dict[str, Any]]:
    out = []
    stack = [plan[0]["Plan"]]
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(node.get("Plans", []))
    return out


async def test_tool_queries_prune_partitions_and_never_seq_scan(
    sessionmaker: SM, world: dict[str, Any]
) -> None:
    """EXPLAIN every telemetry query the tools run, on seeded data: each touches only the
    window's partitions (pruning) and none falls back to a sequential scan of a partition when
    an index can serve it (seq scans disabled to show an index is usable)."""
    t0 = world["t0"]
    w = Window(t0, t0 + 10 * M)
    b = Window(t0 - 10 * M, t0)
    async with sessionmaker() as s:
        await s.execute(text("ANALYZE spans, log_records, metric_points, metric_series"))
        await s.execute(text("SET LOCAL enable_seqscan = off"))
        scope = SystemScope(world["org"], "investigation")
        captured: list[tuple[str, dict[str, Any]]] = []
        original = s.execute

        async def spy(stmt: Any, params: Any = None, **kw: Any) -> Any:
            if hasattr(stmt, "text") and params is not None:
                captured.append((stmt.text, dict(params)))
            return await original(stmt, params, **kw)

        s.execute = spy  # type: ignore[method-assign, assignment]
        logs = LogEvidenceQueries(s, scope)
        traces = TraceEvidenceQueries(s, scope)
        await logs.count(world["orders"], w, 13)
        await logs.samples(world["orders"], w, 13, 2000)
        await logs.exception_signatures([world["orders"]], w, 100)
        await MetricEvidenceQueries(s, scope).deltas(world["orders"], b, w, None, 200)
        await traces.operations([world["gw"], world["orders"]], b, w, 30)
        await traces.span_exceptions([world["orders"]], w, 200)
        sampled = await traces.sample_traces([world["orders"]], w, slowest=3, erroring=2)
        await traces.edge_server_stats(world["gw"], world["orders"], b, w)
        await traces.edge_client_stats(world["orders"], "postgresql", "orders", b, w)
        s.execute = original  # type: ignore[method-assign]
        # count() goes through session.scalar (same predicate as samples); sample_traces runs two.
        assert len(captured) == 9

        # trace_spans (ORM) mirrored as text: one trace by primary-key prefix.
        captured.append(
            (
                "SELECT * FROM spans WHERE org_id = :org AND trace_id = :t"
                " AND start_time >= :s AND start_time < :e ORDER BY start_time, span_id LIMIT 300",
                {"org": world["org"], "t": sampled[0][0], "s": w.start, "e": w.end},
            )
        )
        report = []
        for sql_text, params in captured:
            res = await s.execute(text(f"EXPLAIN (FORMAT JSON) {sql_text}"), params)
            nodes = _plan_nodes(res.scalar_one())
            scans = [
                n
                for n in nodes
                if n.get("Relation Name", "").startswith(
                    ("spans_p", "log_records_p", "metric_points_p")
                )
            ]
            assert scans, sql_text
            per_table: dict[str, set[str]] = {}
            for n in scans:
                per_table.setdefault(n["Relation Name"].split("_p2")[0], set()).add(
                    n["Relation Name"]
                )
            assert all(len(parts) <= 2 for parts in per_table.values()), per_table
            seq = [n["Relation Name"] for n in scans if n["Node Type"] == "Seq Scan"]
            assert not seq, (sql_text, seq)
            indexes = sorted(
                n["Index Name"]
                for n in nodes
                if n.get("Index Name", "").startswith(
                    ("spans_p", "log_records_p", "metric_points_p")
                )
            )
            report.append(indexes)
        # Partition index -> the parent index it was created from.
        parents: dict[str, str] = {
            str(c): str(p)
            for c, p in (
                await s.execute(
                    text(
                        "SELECT c.relname, p.relname FROM pg_inherits i"
                        " JOIN pg_class c ON c.oid = i.inhrelid"
                        " JOIN pg_class p ON p.oid = i.inhparent"
                        " WHERE c.relname = ANY(:names)"
                    ),
                    {"names": sorted({i for r in report for i in r})},
                )
            ).all()
        }
        used = [{parents.get(i, i) for i in r} for r in report]
    # Span queries must use the index whose predicate they repeat (stable across stats); log
    # and metric queries may pick either of their table's indexes, never a seq scan (above).
    assert used[3] == {"ix_spans_server_window", "ix_spans_db_client_window"}  # operations
    assert used[4] == {"ix_spans_error_window"}  # span_exceptions
    assert used[5] == {"ix_spans_server_window"}  # slowest traces
    assert used[6] == {"ix_spans_error_window"}  # erroring traces
    assert "ix_spans_server_window" in used[7]  # edge (callee SERVER spans)
    assert used[8] == {"ix_spans_db_client_window"}  # edge to a database
    assert used[9] == {"pk_spans"}  # one trace
