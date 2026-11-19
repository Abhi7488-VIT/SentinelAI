"""Correlation and incidents (Phase 3 T8) on Postgres over simulated time."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.core.tenancy import SystemScope
from app.services.correlation import Correlator
from app.services.detection import floor_bucket
from app.services.telemetry_maintenance import run_maintenance
from tests.integration.factories import add_member, create_project, create_service, register
from tests.integration.telemetry_support import (
    SM,
    insert_spans,
    rows,
    server_span,
    sql,
    tick,
    ticks,
    traffic,
)

S = timedelta(seconds=1)


@pytest.fixture
async def partitions(sessionmaker: SM, test_settings: Settings) -> None:
    async with sessionmaker() as s:
        await run_maintenance(s, test_settings)


@pytest.fixture
def t0() -> datetime:
    return floor_bucket(datetime.now(UTC)) - timedelta(minutes=20)


NAMES = ("frontend", "api-gateway", "order-service", "payment-service", "batch-a", "batch-b")


@pytest.fixture
async def shop(client: httpx.AsyncClient) -> dict[str, Any]:
    owner = await register(client, "Owner", "Shop Org")
    project = await create_project(client, owner, owner.org_id, "Shop")
    svc = {
        n: (await create_service(client, owner, owner.org_id, project["id"], n))["id"]
        for n in NAMES
    }
    return {"owner": owner, "org": owner.org_id, "project": project, "svc": svc}


async def _deps(client: httpx.AsyncClient, shop: dict[str, Any], src: str, *dst: str) -> None:
    resp = await client.put(
        f"/api/v1/orgs/{shop['org']}/services/{shop['svc'][src]}/dependencies",
        json={"dependencies": [{"target_service_id": shop["svc"][d]} for d in dst]},
        headers=shop["owner"].headers,
    )
    assert resp.status_code == 200, resp.text


async def chain(
    sm: SM,
    org: str,
    services: list[str],
    start: datetime,
    end: datetime,
    *,
    failing: Any,
    exceptions: dict[str, tuple[str, str]] | None = None,
    rps: float = 2.0,
) -> None:
    """One trace per request across `services` (caller first); when `failing(t)` every hop
    returns an error, as a failure propagating up from the last service."""
    per_service: dict[str, list[Any]] = {s: [] for s in services}
    t = start
    while t < end:
        trace, parent = os.urandom(16), None
        bad = bool(failing(t))
        for sid in services:
            exc = (exceptions or {}).get(sid, ("UpstreamError", "upstream failed with 500"))
            row = server_span(t, error=bad, trace=trace, parent=parent, exception=exc)
            per_service[sid].append(row)
            parent = row.span_id
        t += timedelta(seconds=1 / rps)
    for sid, spans in per_service.items():
        await insert_spans(sm, org, sid, spans)


async def _incidents(sm: SM, org: str) -> list[dict[str, Any]]:
    incs = await rows(sm, "SELECT * FROM incidents WHERE org_id = :o ORDER BY number", o=org)
    for inc in incs:
        inc["members"] = await rows(
            sm,
            "SELECT s.name, d.rule, l.reasons, l.score FROM incident_detections l "
            "JOIN detections d ON d.id = l.detection_id JOIN services s ON s.id = d.service_id "
            "WHERE l.incident_id = :i ORDER BY l.score, l.created_at, s.name",
            i=inc["id"],
        )
        inc["services"] = sorted(
            r["name"]
            for r in await rows(
                sm,
                "SELECT s.name FROM incident_services x JOIN services s ON s.id = x.service_id "
                "WHERE x.incident_id = :i",
                i=inc["id"],
            )
        )
    return incs


def _reason_types(member: dict[str, Any]) -> set[str]:
    return {r["type"] for r in member["reasons"]}


async def test_cascading_failure_becomes_one_incident_with_reasons(
    client: httpx.AsyncClient,
    sessionmaker: SM,
    shop: dict[str, Any],
    partitions: None,
    t0: datetime,
) -> None:
    """order-service fails; api-gateway and frontend fail in the same traces; payment-service
    stays healthy. Expect one incident with the three services, payment-service not in it."""
    org, svc = shop["org"], shop["svc"]
    await _deps(client, shop, "frontend", "api-gateway")
    await _deps(client, shop, "api-gateway", "order-service")
    await _deps(client, shop, "order-service", "payment-service")
    path = [svc["frontend"], svc["api-gateway"], svc["order-service"]]
    await chain(
        sessionmaker,
        org,
        path,
        t0 - timedelta(minutes=5),
        t0 + timedelta(minutes=5),
        failing=lambda t: t >= t0,
        exceptions={svc["order-service"]: ("TimeoutError", "QueuePool limit reached 30.0s")},
    )
    await traffic(sessionmaker, org, svc["payment-service"], t0 - 300 * S, t0 + 300 * S, rps=1)
    seen = [*path, svc["payment-service"]]
    results = await ticks(sessionmaker, t0 - 30 * S, t0 + 120 * S, seen=seen)

    (inc,) = await _incidents(sessionmaker, org)
    assert inc["services"] == ["api-gateway", "frontend", "order-service"]
    assert inc["number"] == 1
    assert inc["status"] == "open"
    assert inc["started_at"] == t0 + 15 * S
    assert inc["title"] == "Errors in api-gateway, frontend, order-service"
    assert sum(len(r.incidents_created) for r in results) == 1
    first, *joined = inc["members"]
    assert first["reasons"] == []
    assert len(joined) == 2
    # Each join is explained by structure, never by timing alone. Which pairs share an edge
    # depends on the link order (frontend and order-service have no direct edge).
    for m in joined:
        assert "shared_trace" in _reason_types(m)
        assert m["score"] >= 3
    gateway = next(m for m in inc["members"] if m["name"] == "api-gateway")
    assert gateway is first or "dependency_edge" in _reason_types(gateway)
    events = await rows(
        sessionmaker,
        "SELECT type FROM incident_events WHERE incident_id = :i ORDER BY id",
        i=inc["id"],
    )
    assert [e["type"] for e in events] == [
        "created",
        "detection_added",
        "detection_added",
        "detection_added",
    ]

    # A retried tick (same bucket) and a second correlator pass change nothing.
    await tick(sessionmaker, t0 + 120 * S, seen=seen)
    async with sessionmaker() as s:
        again = await Correlator(s, SystemScope(uuid.UUID(org), "test"), t0 + 120 * S).run()
        await s.commit()
    assert (again.created, again.joined) == ([], [])
    (same,) = await _incidents(sessionmaker, org)
    assert len(same["members"]) == 3


async def test_time_proximity_alone_never_merges(
    sessionmaker: SM, shop: dict[str, Any], partitions: None, t0: datetime
) -> None:
    org, svc = shop["org"], shop["svc"]
    for name, exc in (("batch-a", ("KeyError", "missing a")), ("batch-b", ("OSError", "disk"))):
        await traffic(
            sessionmaker,
            org,
            svc[name],
            t0 - 120 * S,
            t0 + 180 * S,
            error=lambda t, i: t >= t0,
            exception=exc,
        )
    await ticks(sessionmaker, t0, t0 + 90 * S, seen=[svc["batch-a"], svc["batch-b"]])
    incs = await _incidents(sessionmaker, org)
    assert [i["services"] for i in incs] == [["batch-a"], ["batch-b"]] or [
        i["services"] for i in incs
    ] == [["batch-b"], ["batch-a"]]
    assert all(len(i["members"]) == 1 for i in incs)


@pytest.mark.parametrize("reason", ["dependency_edge", "exception_signature", "shared_trace"])
async def test_each_structural_reason_merges_on_its_own(
    client: httpx.AsyncClient,
    sessionmaker: SM,
    shop: dict[str, Any],
    partitions: None,
    t0: datetime,
    reason: str,
) -> None:
    org, a, b = shop["org"], shop["svc"]["batch-a"], shop["svc"]["batch-b"]
    exc_a, exc_b = ("KeyError", "missing a"), ("OSError", "disk full")
    if reason == "dependency_edge":
        await _deps(client, shop, "batch-a", "batch-b")
    if reason == "exception_signature":
        exc_b = exc_a
    if reason == "shared_trace":
        await chain(
            sessionmaker,
            org,
            [a, b],
            t0 - 120 * S,
            t0 + 180 * S,
            failing=lambda t: t >= t0,
            exceptions={a: exc_a, b: exc_b},
        )
    else:
        for sid, exc in ((a, exc_a), (b, exc_b)):
            await traffic(
                sessionmaker,
                org,
                sid,
                t0 - 120 * S,
                t0 + 180 * S,
                error=lambda t, i: t >= t0,
                exception=exc,
            )
    await ticks(sessionmaker, t0, t0 + 90 * S, seen=[a, b])
    (inc,) = await _incidents(sessionmaker, org)
    assert inc["services"] == ["batch-a", "batch-b"]
    assert _reason_types(inc["members"][1]) == {reason}


async def test_ties_go_to_the_older_incident_and_resolved_incidents_take_nothing(
    client: httpx.AsyncClient,
    sessionmaker: SM,
    shop: dict[str, Any],
    partitions: None,
    t0: datetime,
) -> None:
    org, svc, owner = shop["org"], shop["svc"], shop["owner"]
    # batch-a and batch-b fail independently (two incidents); api-gateway depends on both and
    # starts failing a minute later: dependency_edge (score 2) to each → the older incident.
    await _deps(client, shop, "api-gateway", "batch-a", "batch-b")
    for name, exc, start in (
        ("batch-a", ("KeyError", "a"), t0),
        ("batch-b", ("OSError", "b"), t0 + 15 * S),
        ("api-gateway", ("ValueError", "gw"), t0 + 60 * S),
    ):
        await traffic(
            sessionmaker,
            org,
            svc[name],
            t0 - 120 * S,
            t0 + 300 * S,
            error=lambda t, i, s=start: t >= s,
            exception=exc,
        )
    seen = [svc["batch-a"], svc["batch-b"], svc["api-gateway"]]
    await ticks(sessionmaker, t0, t0 + 150 * S, seen=seen)
    incs = await _incidents(sessionmaker, org)
    assert [i["services"] for i in incs] == [["api-gateway", "batch-a"], ["batch-b"]]
    assert incs[0]["members"][1]["score"] == 2

    # Resolve incident 1 by hand; a new detection on batch-a then opens a new incident.
    resp = await client.post(
        f"/api/v1/orgs/{org}/incidents/{incs[0]['id']}/transitions",
        json={"to": "resolved", "note": "fixed"},
        headers=owner.headers,
    )
    assert resp.status_code == 200, resp.text
    await sql(
        sessionmaker,
        "UPDATE detections SET resolved_at = fired_at + interval '1 minute' WHERE service_id = :s",
        s=svc["batch-a"],
    )
    await sql(
        sessionmaker,
        "UPDATE detector_states SET state = 'ok', open_detection_id = NULL WHERE service_id = :s",
        s=svc["batch-a"],
    )
    await ticks(sessionmaker, t0 + 165 * S, t0 + 240 * S, seen=seen)
    incs = await _incidents(sessionmaker, org)
    assert incs[0]["status"] == "resolved"
    assert [i["services"] for i in incs[2:]] == [["batch-a"]]


async def test_all_detections_resolved_event_after_ten_quiet_minutes(
    sessionmaker: SM, shop: dict[str, Any], partitions: None, t0: datetime
) -> None:
    org, a = shop["org"], shop["svc"]["batch-a"]
    await traffic(
        sessionmaker,
        org,
        a,
        t0 - 120 * S,
        t0 + 17 * 60 * S,
        error=lambda t, i: t0 <= t < t0 + 120 * S,
    )
    await ticks(sessionmaker, t0, t0 + 16 * 60 * S, seen=[a])
    (inc,) = await _incidents(sessionmaker, org)
    events = await rows(
        sessionmaker,
        "SELECT type, payload FROM incident_events WHERE incident_id = :i ORDER BY id",
        i=inc["id"],
    )
    assert [e["type"] for e in events][-1] == "all_detections_resolved"
    assert inc["status"] == "open"  # the system never resolves an incident itself
    assert inc["detections_resolved_at"] is not None


async def test_incident_api_list_detail_transitions_events(
    client: httpx.AsyncClient,
    sessionmaker: SM,
    shop: dict[str, Any],
    partitions: None,
    t0: datetime,
) -> None:
    org, svc, owner = shop["org"], shop["svc"], shop["owner"]
    await traffic(
        sessionmaker,
        org,
        svc["order-service"],
        t0 - 120 * S,
        t0 + 180 * S,
        error=lambda t, i: t >= t0,
    )
    await ticks(sessionmaker, t0, t0 + 90 * S, seen=[svc["order-service"]])
    base = f"/api/v1/orgs/{org}/incidents"
    h = owner.headers

    listed = (await client.get(base, params={"status": "open"}, headers=h)).json()
    (item,) = listed["items"]
    assert item["services"][0]["name"] == "order-service"
    for params, expected in (
        ({"q": "order"}, 1),
        ({"q": "#1"}, 1),
        ({"q": "nothing-like-this"}, 0),
        ({"q": "100%_"}, 0),  # wildcards are literal
        ({"severity": "critical"}, 0),
        ({"service_id": svc["order-service"]}, 1),
        ({"service_id": svc["frontend"]}, 0),
        ({"project_id": shop["project"]["id"]}, 1),
        ({"from": (t0 + 60 * S).isoformat()}, 0),
        ({"to": (t0 + 60 * S).isoformat()}, 1),
        ({"status": "resolved"}, 0),
    ):
        got = (await client.get(base, params=params, headers=h)).json()["items"]
        assert len(got) == expected, params

    detail = (await client.get(f"{base}/{item['id']}", headers=h)).json()
    (member,) = detail["detections"]
    assert member["detection"]["rule"] == "error_rate"
    assert member["reasons"] == []
    assert member["detection"]["explanation"].startswith("order-service: error rate")

    # Viewer cannot transition; invalid transition is 409; valid ones are recorded.
    viewer = await register(client, "Vic", "Vic Org")
    await add_member(client, owner, org, viewer, "viewer")
    url = f"{base}/{item['id']}/transitions"
    assert (
        await client.post(url, json={"to": "resolved"}, headers=viewer.headers)
    ).status_code == 403
    bad = await client.post(url, json={"to": "mitigated"}, headers=h)
    assert bad.status_code == 409
    assert bad.json()["error"]["code"] == "invalid_transition"
    for to in ("investigating", "mitigated", "resolved", "open"):
        resp = await client.post(url, json={"to": to, "note": f"-> {to}"}, headers=h)
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == to
        assert (resp.json()["resolved_at"] is not None) == (to == "resolved")

    page1 = (await client.get(f"{base}/{item['id']}/events", params={"limit": 2}, headers=h)).json()
    assert [e["type"] for e in page1["items"]] == ["created", "detection_added"]
    page2 = (
        await client.get(
            f"{base}/{item['id']}/events",
            params={"after_id": page1["next_cursor"], "limit": 10},
            headers=h,
        )
    ).json()
    assert [e["payload"]["to"] for e in page2["items"]] == [
        "investigating",
        "mitigated",
        "resolved",
        "open",
    ]
    assert all(e["actor"] == "user" and e["actor_user_id"] == owner.user_id for e in page2["items"])
    assert page1["items"][-1]["id"] < page2["items"][0]["id"]
    audit = (await client.get(f"/api/v1/orgs/{org}/audit-logs", headers=h)).json()["items"]
    assert sum(a["action"] == "incident.status_changed" for a in audit) == 4

    # Another org: every incident route is 404 for these ids.
    eve = await register(client, "Eve", "Eve Org")
    other = f"/api/v1/orgs/{eve.org_id}/incidents/{item['id']}"
    for resp in (
        await client.get(other, headers=eve.headers),
        await client.get(f"{other}/events", headers=eve.headers),
        await client.post(f"{other}/transitions", json={"to": "resolved"}, headers=eve.headers),
    ):
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "incident_not_found"
    assert (await client.get(f"/api/v1/orgs/{eve.org_id}/incidents", headers=eve.headers)).json()[
        "items"
    ] == []
