"""Dependency graph (Phase 3 T6): evidence-only edges, declared sync, observed discovery, API."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.tenancy import SystemScope
from app.ingest.batch import SpanRow
from app.models.telemetry import SpanKind, SpanStatus
from app.repositories import service_graph as graph_repo
from app.repositories.telemetry import SpanRepository
from app.services import service_graph
from app.services.service_graph import discover_edges
from app.services.telemetry_maintenance import run_maintenance
from tests.integration.factories import (
    Actor,
    add_member,
    create_project,
    create_service,
    register,
)

SM = async_sessionmaker[AsyncSession]


@pytest.fixture
async def partitions(sessionmaker: SM, test_settings: Settings) -> None:
    async with sessionmaker() as s:
        await run_maintenance(s, test_settings)


async def _sql(sm: SM, sql: str, **params: Any) -> Any:
    async with sm() as s:
        result = await s.execute(text(sql), params)
        await s.commit()
        return result


async def _scalar(sm: SM, sql: str, **params: Any) -> Any:
    async with sm() as s:
        return (await s.execute(text(sql), params)).scalar()


def _us(t: datetime) -> int:
    return int(t.timestamp() * 1_000_000)


def _span(
    trace: bytes,
    span: bytes,
    parent: bytes | None,
    kind: SpanKind,
    at: datetime,
    *,
    db_system: str | None = None,
    attributes: dict[str, Any] | None = None,
    server_address: str | None = None,
) -> SpanRow:
    return SpanRow(
        trace_id=trace,
        span_id=span,
        parent_span_id=parent,
        name="op",
        kind=int(kind),
        start_us=_us(at),
        end_us=_us(at + timedelta(milliseconds=5)),
        duration_ms=5.0,
        status_code=int(SpanStatus.UNSET),
        status_message=None,
        http_method=None,
        http_route=None,
        http_status_code=None,
        db_system=db_system,
        server_address=server_address,
        peer_service=None,
        exception_type=None,
        exception_message=None,
        service_instance_id=None,
        service_version=None,
        attributes=attributes or {},
    )


async def _insert_spans(sm: SM, org_id: str, service_id: str, rows: list[SpanRow]) -> None:
    async with sm() as s:
        scope = SystemScope(org_id=uuid.UUID(org_id), actor="test")
        await SpanRepository(s, scope).insert_many(uuid.UUID(service_id), rows)
        await s.commit()


async def _mark_seen(sm: SM, *service_ids: str, at: datetime) -> None:
    for sid in service_ids:
        await _sql(sm, "UPDATE services SET last_seen_at = :t WHERE id = :id", t=at, id=sid)


async def _call(
    sm: SM,
    org: str,
    caller: str,
    callee: str,
    at: datetime,
    *,
    trace: bytes | None = None,
) -> bytes:
    """caller SERVER → caller CLIENT → callee SERVER, in one trace. Returns the trace id."""
    trace = trace or os.urandom(16)
    root, client_span, server_span = os.urandom(8), os.urandom(8), os.urandom(8)
    await _insert_spans(
        sm,
        org,
        caller,
        [
            _span(trace, root, None, SpanKind.SERVER, at),
            _span(trace, client_span, root, SpanKind.CLIENT, at + timedelta(milliseconds=1)),
        ],
    )
    await _insert_spans(
        sm,
        org,
        callee,
        [_span(trace, server_span, client_span, SpanKind.SERVER, at + timedelta(milliseconds=2))],
    )
    return trace


async def _db_call(
    sm: SM, org: str, service: str, at: datetime, namespace: str | None, address: str = "pg"
) -> None:
    attrs = {"db.namespace": namespace} if namespace is not None else {}
    await _insert_spans(
        sm,
        org,
        service,
        [
            _span(
                os.urandom(16),
                os.urandom(8),
                None,
                SpanKind.CLIENT,
                at,
                db_system="postgresql",
                attributes=attrs,
                server_address=address,
            )
        ],
    )


async def _edges(sm: SM, org: str) -> list[dict[str, Any]]:
    async with sm() as s:
        rows = await s.execute(
            text(
                "SELECT e.source, src.name AS src, tgt.name AS tgt, e.target_external, "
                "e.call_count, e.sample_trace_id, e.declared_dependency_id "
                "FROM service_edges e JOIN services src ON src.id = e.source_service_id "
                "LEFT JOIN services tgt ON tgt.id = e.target_service_id "
                "WHERE e.org_id = :o ORDER BY 1, 2, 3, 4"
            ),
            {"o": org},
        )
        return [dict(r._mapping) for r in rows]


@pytest.fixture
async def shop(client: httpx.AsyncClient) -> dict[str, Any]:
    owner = await register(client, "Owner", "Shop Org")
    project = await create_project(client, owner, owner.org_id, "Shop")
    svcs = {
        name: await create_service(client, owner, owner.org_id, project["id"], name)
        for name in ("frontend", "api-gateway", "order-service")
    }
    return {"owner": owner, "project": project, "svc": {k: v["id"] for k, v in svcs.items()}}


# ---- evidence is enforced by the database --------------------------------------------------


async def test_edge_without_evidence_is_rejected_by_the_database(
    sessionmaker: SM, shop: dict[str, Any]
) -> None:
    org, svc = shop["owner"].org_id, shop["svc"]
    base = (
        "INSERT INTO service_edges (org_id, source_service_id, target_service_id, source, "
        "first_seen_at, last_seen_at{cols}) VALUES (:o, :s, :t, :src, now(), now(){vals})"
    )
    params = {"o": org, "s": svc["frontend"], "t": svc["api-gateway"]}
    # observed without a sample trace; declared without a dependency row
    for source in ("observed", "declared"):
        with pytest.raises(IntegrityError, match="ck_service_edges_evidence_required"):
            await _sql(sessionmaker, base.format(cols="", vals=""), src=source, **params)
    # observed carrying a dependency id instead of a trace
    with pytest.raises(IntegrityError, match="ck_service_edges_evidence_required"):
        await _sql(
            sessionmaker,
            base.format(cols=", declared_dependency_id", vals=", gen_random_uuid()"),
            src="observed",
            **params,
        )
    # a declared edge citing a dependency id that does not exist
    with pytest.raises(IntegrityError, match="fk_service_edges_declared_dependency"):
        await _sql(
            sessionmaker,
            base.format(cols=", declared_dependency_id", vals=", gen_random_uuid()"),
            src="declared",
            **params,
        )
    assert await _edges(sessionmaker, org) == []


async def test_edge_cannot_point_into_another_tenant(
    client: httpx.AsyncClient, sessionmaker: SM, shop: dict[str, Any]
) -> None:
    other = await register(client, "Mallory", "Other Org")
    other_project = await create_project(client, other, other.org_id, "X")
    foreign = await create_service(client, other, other.org_id, other_project["id"], "x")
    with pytest.raises(IntegrityError, match="fk_service_edges_target"):
        await _sql(
            sessionmaker,
            "INSERT INTO service_edges (org_id, source_service_id, target_service_id, source, "
            "first_seen_at, last_seen_at, sample_trace_id) "
            "VALUES (:o, :s, :t, 'observed', now(), now(), :trace)",
            o=shop["owner"].org_id,
            s=shop["svc"]["frontend"],
            t=foreign["id"],
            trace=os.urandom(16),
        )


# ---- declared edges follow the declared dependencies ---------------------------------------


async def _put_deps(
    client: httpx.AsyncClient, actor: Actor, service_id: str, deps: list[dict[str, Any]]
) -> None:
    resp = await client.put(
        f"/api/v1/orgs/{actor.org_id}/services/{service_id}/dependencies",
        json={"dependencies": deps},
        headers=actor.headers,
    )
    assert resp.status_code == 200, resp.text


async def test_declared_edges_follow_dependency_changes(
    client: httpx.AsyncClient, sessionmaker: SM, shop: dict[str, Any]
) -> None:
    owner, svc = shop["owner"], shop["svc"]
    await _put_deps(
        client,
        owner,
        svc["api-gateway"],
        [{"target_service_id": svc["order-service"]}, {"target_external": "redis:sessions"}],
    )
    edges = await _edges(sessionmaker, owner.org_id)
    assert [(e["source"], e["src"], e["tgt"], e["target_external"]) for e in edges] == [
        ("declared", "api-gateway", "order-service", None),
        ("declared", "api-gateway", None, "redis:sessions"),
    ]
    dep_ids = await _scalar(
        sessionmaker,
        "SELECT array_agg(id ORDER BY id) FROM service_dependencies WHERE service_id = :s",
        s=svc["api-gateway"],
    )
    assert sorted(e["declared_dependency_id"] for e in edges) == sorted(dep_ids)

    # Replacing the set replaces the edges; an empty set removes them.
    await _put_deps(client, owner, svc["api-gateway"], [{"target_external": "postgresql:x"}])
    edges = await _edges(sessionmaker, owner.org_id)
    assert [(e["src"], e["target_external"]) for e in edges] == [("api-gateway", "postgresql:x")]
    await _put_deps(client, owner, svc["api-gateway"], [])
    assert await _edges(sessionmaker, owner.org_id) == []


# ---- observed discovery ---------------------------------------------------------------------


async def test_discovers_service_and_database_edges_with_sample_traces(
    sessionmaker: SM, shop: dict[str, Any], partitions: None
) -> None:
    org, svc = shop["owner"].org_id, shop["svc"]
    now = datetime.now(UTC)
    t = now - timedelta(seconds=30)
    traces = [await _call(sessionmaker, org, svc["frontend"], svc["api-gateway"], t)]
    traces.append(await _call(sessionmaker, org, svc["frontend"], svc["api-gateway"], t))
    await _call(sessionmaker, org, svc["api-gateway"], svc["order-service"], t)
    for _ in range(3):
        await _db_call(sessionmaker, org, svc["order-service"], t, "orders")
    await _db_call(sessionmaker, org, svc["order-service"], t, None, address="cache-host")
    await _mark_seen(sessionmaker, *svc.values(), at=now)

    async with sessionmaker() as s:
        result = await discover_edges(s, now=now)
    assert result.ran
    assert (result.orgs, result.services_scanned) == (1, 3)
    assert (result.service_edges, result.external_edges) == (2, 2)

    edges = await _edges(sessionmaker, org)
    summary = [(e["src"], e["tgt"], e["target_external"], e["call_count"]) for e in edges]
    assert summary == [
        ("api-gateway", "order-service", None, 1),
        ("frontend", "api-gateway", None, 2),
        ("order-service", None, "postgresql:cache-host", 1),  # no db.namespace → address
        ("order-service", None, "postgresql:orders", 3),
    ]
    assert all(e["source"] == "observed" and len(e["sample_trace_id"]) == 16 for e in edges)
    assert edges[1]["sample_trace_id"] in traces

    # A retried / repeated run over the same window changes nothing.
    async with sessionmaker() as s:
        await discover_edges(s, now=now)
    assert await _edges(sessionmaker, org) == edges


async def test_spans_outside_the_window_or_within_one_service_make_no_edge(
    sessionmaker: SM, shop: dict[str, Any], partitions: None
) -> None:
    org, svc = shop["owner"].org_id, shop["svc"]
    now = datetime.now(UTC)
    # Old call (outside the 2-minute window) and a parent/child pair inside one service.
    await _call(sessionmaker, org, svc["frontend"], svc["api-gateway"], now - timedelta(minutes=5))
    trace, a, b = os.urandom(16), os.urandom(8), os.urandom(8)
    await _insert_spans(
        sessionmaker,
        org,
        svc["frontend"],
        [
            _span(trace, a, None, SpanKind.SERVER, now - timedelta(seconds=10)),
            _span(trace, b, a, SpanKind.SERVER, now - timedelta(seconds=9)),
        ],
    )
    await _mark_seen(sessionmaker, *svc.values(), at=now)
    async with sessionmaker() as s:
        result = await discover_edges(s, now=now)
    assert result.ran
    assert result.service_edges == 0
    assert await _edges(sessionmaker, org) == []


async def test_callers_in_other_projects_or_archived_are_skipped(
    client: httpx.AsyncClient, sessionmaker: SM, shop: dict[str, Any], partitions: None
) -> None:
    owner, svc = shop["owner"], shop["svc"]
    other_project = await create_project(client, owner, owner.org_id, "Other")
    outsider = await create_service(client, owner, owner.org_id, other_project["id"], "batch")
    now = datetime.now(UTC)
    t = now - timedelta(seconds=20)
    await _call(sessionmaker, owner.org_id, outsider["id"], svc["order-service"], t)
    await _call(sessionmaker, owner.org_id, svc["frontend"], svc["api-gateway"], t)
    await _mark_seen(sessionmaker, *svc.values(), outsider["id"], at=now)
    resp = await client.delete(
        f"/api/v1/orgs/{owner.org_id}/services/{svc['frontend']}", headers=owner.headers
    )
    assert resp.status_code == 204

    async with sessionmaker() as s:
        result = await discover_edges(s, now=now)
    assert result.skipped == {"cross_project": 1, "caller_inactive": 1}
    assert await _edges(sessionmaker, owner.org_id) == []


async def test_untrusted_external_names_are_validated_and_capped(
    sessionmaker: SM,
    shop: dict[str, Any],
    partitions: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org, order = shop["owner"].org_id, shop["svc"]["order-service"]
    monkeypatch.setattr(service_graph, "MAX_OBSERVED_EXTERNALS_PER_SERVICE", 2)
    now = datetime.now(UTC)
    t = now - timedelta(seconds=20)
    for namespace in ("a", "b", "c", "bad name; DROP TABLE", "{{inject}}"):
        await _db_call(sessionmaker, org, order, t, namespace)
    await _mark_seen(sessionmaker, order, at=now)

    async with sessionmaker() as s:
        result = await discover_edges(s, now=now)
    assert result.skipped == {"invalid_external_name": 2, "external_limit": 1}
    names = [e["target_external"] for e in await _edges(sessionmaker, org)]
    assert len(names) == 2
    assert set(names) <= {"postgresql:a", "postgresql:b", "postgresql:c"}

    # Names already known keep being refreshed at the cap; new ones still are not added.
    async with sessionmaker() as s:
        again = await discover_edges(s, now=now)
    assert again.external_edges == 2
    assert again.skipped["external_limit"] == 1
    assert sorted(e["target_external"] for e in await _edges(sessionmaker, org)) == sorted(names)


async def test_observed_edges_expire_but_declared_edges_do_not(
    client: httpx.AsyncClient, sessionmaker: SM, shop: dict[str, Any], partitions: None
) -> None:
    owner, svc = shop["owner"], shop["svc"]
    now = datetime.now(UTC)
    await _put_deps(client, owner, svc["frontend"], [{"target_service_id": svc["api-gateway"]}])
    await _call(sessionmaker, owner.org_id, svc["frontend"], svc["api-gateway"], now)
    await _mark_seen(sessionmaker, *svc.values(), at=now)
    async with sessionmaker() as s:
        await discover_edges(s, now=now + timedelta(seconds=30))
    assert [e["source"] for e in await _edges(sessionmaker, owner.org_id)] == [
        "declared",
        "observed",
    ]
    later = now + service_graph.OBSERVED_EDGE_TTL + timedelta(minutes=5)
    await _mark_seen(sessionmaker, svc["frontend"], at=later)  # org still active
    async with sessionmaker() as s:
        result = await discover_edges(s, now=later)
    assert result.expired == 1
    assert [e["source"] for e in await _edges(sessionmaker, owner.org_id)] == ["declared"]


async def test_overlapping_run_skips_while_locked(sessionmaker: SM) -> None:
    async with sessionmaker() as holder:
        assert await graph_repo.try_discovery_lock(holder)
        async with sessionmaker() as s:
            assert (await discover_edges(s)).ran is False
        await holder.rollback()
    async with sessionmaker() as s:
        assert (await discover_edges(s)).ran is True


# ---- API ------------------------------------------------------------------------------------


async def test_graph_api_returns_nodes_and_both_edge_sources(
    client: httpx.AsyncClient, sessionmaker: SM, shop: dict[str, Any], partitions: None
) -> None:
    owner, svc, project = shop["owner"], shop["svc"], shop["project"]
    viewer = await register(client, "Vic", "Vic Org")
    await add_member(client, owner, owner.org_id, viewer, "viewer")
    await _put_deps(
        client,
        owner,
        svc["frontend"],
        [{"target_service_id": svc["api-gateway"]}, {"target_external": "postgresql:orders"}],
    )
    now = datetime.now(UTC)
    trace = await _call(sessionmaker, owner.org_id, svc["frontend"], svc["api-gateway"], now)
    await _mark_seen(sessionmaker, *svc.values(), at=now)
    async with sessionmaker() as s:
        await discover_edges(s, now=now + timedelta(seconds=5))

    resp = await client.get(
        f"/api/v1/orgs/{owner.org_id}/service-graph",
        params={"project_id": project["id"]},
        headers=viewer.headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["truncated"] is False
    nodes = {n["id"]: n for n in body["nodes"]}
    assert sorted(n["name"] for n in nodes.values()) == [
        "api-gateway",
        "frontend",
        "order-service",
        "postgresql:orders",
    ]
    assert nodes["external:postgresql:orders"]["kind"] == "external"
    fe = f"service:{svc['frontend']}"
    gw = f"service:{svc['api-gateway']}"
    pairs = sorted((e["from_node"], e["to_node"], e["source"]) for e in body["edges"])
    assert pairs == sorted(
        [
            (fe, gw, "declared"),
            (fe, gw, "observed"),
            (fe, "external:postgresql:orders", "declared"),
        ]
    )
    observed = next(e for e in body["edges"] if e["source"] == "observed")
    assert observed["sample_trace_id"] == trace.hex()
    assert observed["call_count"] == 1
    assert observed["declared_dependency_id"] is None


async def test_graph_api_hides_archived_services_and_their_edges(
    client: httpx.AsyncClient, shop: dict[str, Any]
) -> None:
    owner, svc, project = shop["owner"], shop["svc"], shop["project"]
    await _put_deps(client, owner, svc["frontend"], [{"target_service_id": svc["api-gateway"]}])
    resp = await client.delete(
        f"/api/v1/orgs/{owner.org_id}/services/{svc['api-gateway']}", headers=owner.headers
    )
    assert resp.status_code == 204
    body = (
        await client.get(
            f"/api/v1/orgs/{owner.org_id}/service-graph",
            params={"project_id": project["id"]},
            headers=owner.headers,
        )
    ).json()
    assert sorted(n["name"] for n in body["nodes"]) == ["frontend", "order-service"]
    assert body["edges"] == []


async def test_graph_api_foreign_project_is_404(
    client: httpx.AsyncClient, shop: dict[str, Any]
) -> None:
    other = await register(client, "Eve", "Eve Org")
    resp = await client.get(
        f"/api/v1/orgs/{other.org_id}/service-graph",
        params={"project_id": shop["project"]["id"]},
        headers=other.headers,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "project_not_found"
