"""Detection engine end to end on Postgres (Phase 3 T7): rollups, rules, state persistence,
idempotent ticks, evidence, deploy_change, health status, API."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.services.detection import floor_bucket
from app.services.telemetry_maintenance import run_maintenance
from tests.integration.factories import create_project, create_service, register
from tests.integration.telemetry_support import SM, rows, sql, tick, ticks, traffic

S = timedelta(seconds=1)


@pytest.fixture
async def partitions(sessionmaker: SM, test_settings: Settings) -> None:
    async with sessionmaker() as s:
        await run_maintenance(s, test_settings)


@pytest.fixture
def t0() -> datetime:
    """Fault start, on the 15-s grid, far enough in the past that every simulated tick is."""
    return floor_bucket(datetime.now(UTC)) - timedelta(minutes=15)


@pytest.fixture
async def shop(client: httpx.AsyncClient) -> dict[str, Any]:
    owner = await register(client, "Owner", "Shop Org")
    project = await create_project(client, owner, owner.org_id, "Shop")
    svcs = {
        name: (await create_service(client, owner, owner.org_id, project["id"], name))["id"]
        for name in ("order-service", "api-gateway")
    }
    return {"owner": owner, "org": owner.org_id, "project": project, "svc": svcs}


async def _detections(sm: SM, org: str, rule: str | None = None) -> list[dict[str, Any]]:
    return await rows(
        sm,
        "SELECT d.*, s.name AS service FROM detections d JOIN services s ON s.id = d.service_id "
        "WHERE d.org_id = :o AND (CAST(:r AS text) IS NULL OR d.rule = :r) "
        "ORDER BY d.started_at, d.rule",
        o=org,
        r=rule,
    )


async def _state(sm: SM, service_id: str, rule: str) -> dict[str, Any]:
    (state,) = await rows(
        sm,
        "SELECT * FROM detector_states WHERE service_id = :s AND rule = :r",
        s=service_id,
        r=rule,
    )
    return state


async def test_error_spike_fires_once_escalates_resolves_and_links_the_deploy(
    sessionmaker: SM, shop: dict[str, Any], partitions: None, t0: datetime
) -> None:
    org, order = shop["org"], shop["svc"]["order-service"]
    # Half of the requests fail for 5 minutes from t0; a deployment 60 s before.
    await traffic(
        sessionmaker,
        org,
        order,
        t0 - timedelta(minutes=10),
        t0 + timedelta(minutes=9),
        error=lambda t, i: t0 <= t < t0 + 300 * S and i % 2 == 0,
    )
    await sql(
        sessionmaker,
        "INSERT INTO deployments (org_id, service_id, deployment_id, status, version, "
        "previous_version, ts) VALUES (:o, :s, 'dep-1', 'in_progress', '1.1', '1.0', :t), "
        "(:o, :s, 'dep-1', 'succeeded', '1.1', '1.0', :t2)",
        o=org,
        s=order,
        t=t0 - 60 * S,
        t2=t0 - 55 * S,
    )

    await ticks(sessionmaker, t0 - 60 * S, t0 + 30 * S, seen=[order])
    # Restart mid-pending: the state is in Postgres (every tick runs in a fresh session).
    pending = await _state(sessionmaker, order, "error_rate")
    assert (pending["state"], pending["since"]) == ("pending", t0 + 15 * S)
    assert await _detections(sessionmaker, org) == []

    results = await ticks(sessionmaker, t0 + 45 * S, t0 + 480 * S, seen=[order])

    (det,) = await _detections(sessionmaker, org, "error_rate")
    assert det["service"] == "order-service"
    assert (det["started_at"], det["fired_at"]) == (t0 + 15 * S, t0 + 75 * S)
    assert det["resolved_at"] == t0 + 480 * S  # clear from t0+360 held 120 s
    assert det["severity"] == "critical"  # >= 25% and firing >= 3 min
    assert det["peak_value"] == pytest.approx(0.5)
    assert det["threshold"] == 0.05
    assert det["explanation"].startswith("order-service: error rate 50.0% (60 of 120 server")
    assert (
        "Top exception: `TimeoutError: QueuePool limit of size # reached, #s`"
        in (det["explanation"])
    )
    ev = det["evidence"]
    assert ev["facts"]["baseline_ratio"] == 0.0
    assert 1 <= len(ev["sample_trace_ids"]) <= 20
    assert ev["exception_signatures"][0]["type"] == "TimeoutError"
    # Every sample trace is a real error span of this service inside the window.
    found = await rows(
        sessionmaker,
        "SELECT count(DISTINCT trace_id) AS n FROM spans WHERE service_id = :s AND "
        "status_code = 2 AND trace_id = ANY(:ids)",
        s=order,
        ids=[bytes.fromhex(x) for x in ev["sample_trace_ids"]],
    )
    assert found[0]["n"] == len(ev["sample_trace_ids"])

    (deploy,) = await _detections(sessionmaker, org, "deploy_change")
    assert deploy["started_at"] == t0 - 60 * S
    assert deploy["evidence"]["deployment_id"] == "dep-1"
    assert deploy["evidence"]["linked_detection_ids"] == [str(det["id"])]
    assert deploy["resolved_at"] == t0 + 480 * S
    assert "deployment dep-1 (1.0 -> 1.1)" in deploy["explanation"]

    # Nothing else fired: steady latency, no metrics, healthy heartbeat.
    assert {d["rule"] for d in await _detections(sessionmaker, org)} == {
        "error_rate",
        "deploy_change",
    }
    assert sum(len(r.fired) for r in results) == 2

    # A retried tick for an already evaluated bucket changes nothing.
    again = await tick(sessionmaker, t0 + 480 * S, seen=[order])
    assert again.evaluated == 0
    assert again.fired == []
    assert len(await _detections(sessionmaker, org)) == 2


async def test_short_spike_never_fires(
    sessionmaker: SM, shop: dict[str, Any], partitions: None, t0: datetime
) -> None:
    org, order = shop["org"], shop["svc"]["order-service"]
    await traffic(
        sessionmaker,
        org,
        order,
        t0 - timedelta(minutes=5),
        t0 + timedelta(minutes=4),
        error=lambda t, i: t0 <= t < t0 + 10 * S,  # every request fails for 10 s
    )
    await ticks(sessionmaker, t0 - 30 * S, t0 + 30 * S, seen=[order])
    assert (await _state(sessionmaker, order, "error_rate"))["state"] == "pending"
    await ticks(sessionmaker, t0 + 45 * S, t0 + 180 * S, seen=[order])
    assert await _detections(sessionmaker, org) == []
    assert (await _state(sessionmaker, order, "error_rate"))["state"] == "ok"


async def test_latency_warms_up_then_fires_against_the_median_baseline(
    sessionmaker: SM, shop: dict[str, Any], partitions: None, t0: datetime
) -> None:
    org, gw = shop["org"], shop["svc"]["api-gateway"]
    await traffic(
        sessionmaker,
        org,
        gw,
        t0 - timedelta(minutes=32),
        t0 + timedelta(minutes=4),
        rps=1,
        duration=lambda t: 500.0 if t >= t0 else 20.0 + (t.second % 5),
    )
    await tick(sessionmaker, t0 - timedelta(minutes=25), seen=[gw])
    warming = await _state(sessionmaker, gw, "latency_shift")
    assert warming["state"] == "warming_up"

    await ticks(sessionmaker, t0 - 60 * S, t0 + 150 * S, seen=[gw])
    (det,) = await _detections(sessionmaker, org, "latency_shift")
    assert (det["started_at"], det["fired_at"]) == (t0 + 15 * S, t0 + 135 * S)
    assert det["baseline_value"] == pytest.approx(22.8, abs=2.5)
    assert det["threshold"] == pytest.approx(det["baseline_value"] + 100)
    assert det["observed_value"] == pytest.approx(500.0)
    assert det["evidence"]["facts"]["baseline_minutes"] >= 10
    assert "p95 latency 500 ms" in det["explanation"]
    assert len(det["evidence"]["sample_trace_ids"]) >= 1


async def _series(sm: SM, org: str, svc: str, name: str, attrs: dict[str, str]) -> int:
    digest = hashlib.sha256(json.dumps([svc, name, attrs], sort_keys=True).encode()).digest()
    (row,) = await rows(
        sm,
        "INSERT INTO metric_series (org_id, service_id, series_hash, name, kind, is_monotonic, "
        "temporality, attributes) VALUES (:o, :s, :h, :n, 2, false, 2, CAST(:a AS jsonb)) "
        "RETURNING id",
        o=org,
        s=svc,
        h=digest,
        n=name,
        a=json.dumps(attrs),
    )
    return int(row["id"])


async def test_saturated_pool_with_waiters_fires(
    sessionmaker: SM, shop: dict[str, Any], partitions: None, t0: datetime
) -> None:
    org, order = shop["org"], shop["svc"]["order-service"]
    base = {"service.instance.id": "i-1", "db.client.connection.pool.name": "orders"}
    used = await _series(
        sessionmaker,
        org,
        order,
        "db.client.connection.count",
        {**base, "db.client.connection.state": "used"},
    )
    mx = await _series(sessionmaker, org, order, "db.client.connection.max", base)
    pending = await _series(sessionmaker, org, order, "db.client.connection.pending_requests", base)
    t = t0 - 120 * S
    while t < t0 + 240 * S:
        bad = t >= t0
        for sid, value in ((used, 10 if bad else 2), (mx, 10), (pending, 3 if bad else 0)):
            await sql(
                sessionmaker,
                "INSERT INTO metric_points (org_id, series_id, ts, value) VALUES (:o, :s, :t, :v)",
                o=org,
                s=sid,
                t=t,
                v=value,
            )
        t += 10 * S

    # Ticks' now = eval_end + 30 s; metrics are read up to now.
    await ticks(sessionmaker, t0 - 60 * S, t0 + 60 * S, seen=[order])
    (det,) = await _detections(sessionmaker, org, "resource_saturation")
    assert det["metric"] == "pool_utilization"
    assert det["observed_value"] == 1.0
    assert det["evidence"]["facts"]["pools"][0]["pending"] == 3
    assert "connection pool `orders` 10/10 in use (100.0%) with 3 pending" in det["explanation"]
    # Fired 60 s after the first saturated reading was visible (eval_end t0-30 → now t0).
    assert det["fired_at"] - det["started_at"] == 60 * S


async def test_heartbeat_loss_and_crash_loop(
    sessionmaker: SM, shop: dict[str, Any], partitions: None, t0: datetime
) -> None:
    org, order, gw = shop["org"], shop["svc"]["order-service"], shop["svc"]["api-gateway"]
    # api-gateway: 3 new instances within 2 minutes.
    for n, offset in enumerate((0, 40, 80)):
        start = t0 + offset * S
        await traffic(sessionmaker, org, gw, start, start + 30 * S, rps=1, instance=f"gw-{n}")
    await ticks(sessionmaker, t0 - 30 * S, t0 + 120 * S, seen=[order, gw])
    (loop,) = await _detections(sessionmaker, org, "health_check")
    assert loop["service"] == "api-gateway"
    assert loop["metric"] == "new_instances_5m"
    assert "3 new service instances within 5 min" in loop["explanation"]

    # order-service goes silent: heartbeat lost after 90 s, critical after 3 more minutes.
    silent_from = t0 + 120 * S + 30 * S  # last_seen_at of the last tick above
    await ticks(sessionmaker, t0 + 135 * S, t0 + 420 * S, seen=[gw])
    lost = [
        d
        for d in await _detections(sessionmaker, org, "health_check")
        if d["service"] == "order-service"
    ]
    assert len(lost) == 1
    assert lost[0]["fired_at"] + 30 * S - silent_from >= 90 * S
    assert lost[0]["severity"] == "critical"
    statuses = {
        r["name"]: r["health_status"]
        for r in await rows(sessionmaker, "SELECT name, health_status FROM services")
    }
    assert statuses["order-service"] == "unhealthy"


async def test_detections_api_filters_and_isolation(
    client: httpx.AsyncClient,
    sessionmaker: SM,
    shop: dict[str, Any],
    partitions: None,
    t0: datetime,
) -> None:
    owner, org, order = shop["owner"], shop["org"], shop["svc"]["order-service"]
    await traffic(
        sessionmaker,
        org,
        order,
        t0 - timedelta(minutes=3),
        t0 + timedelta(minutes=3),
        error=lambda t, i: t >= t0,
    )
    await ticks(sessionmaker, t0, t0 + 90 * S, seen=[order])
    base = f"/api/v1/orgs/{org}"
    listed = await client.get(
        f"{base}/detections",
        params={"rule": "error_rate", "status": "open", "service_id": order},
        headers=owner.headers,
    )
    assert listed.status_code == 200, listed.text
    (item,) = listed.json()["items"]
    assert item["rule"] == "error_rate"
    assert item["resolved_at"] is None
    resolved = await client.get(
        f"{base}/detections", params={"status": "resolved"}, headers=owner.headers
    )
    assert resolved.json()["items"] == []
    one = await client.get(f"{base}/detections/{item['id']}", headers=owner.headers)
    assert one.json()["explanation"] == item["explanation"]
    states = await client.get(
        f"{base}/detector-states", params={"service_id": order}, headers=owner.headers
    )
    assert {s["rule"]: s["state"] for s in states.json()["items"]}["error_rate"] == "firing"

    # Another org's owner cannot read it through their own org either.
    eve = await register(client, "Eve", "Eve Org")
    other = await client.get(
        f"/api/v1/orgs/{eve.org_id}/detections/{item['id']}", headers=eve.headers
    )
    assert other.status_code == 404
    assert other.json()["error"]["code"] == "detection_not_found"
    assert (await client.get(f"/api/v1/orgs/{eve.org_id}/detections", headers=eve.headers)).json()[
        "items"
    ] == []
