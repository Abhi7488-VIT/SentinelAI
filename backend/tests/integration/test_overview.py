"""Overview and per-service ingest usage APIs (Phase 3 T9 backend) on Postgres."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.services.detection import floor_bucket
from app.services.telemetry_maintenance import run_maintenance
from tests.integration.factories import (
    add_member,
    create_api_key,
    create_project,
    create_service,
    register,
)
from tests.integration.telemetry_support import SM, sql, ticks, traffic

S = timedelta(seconds=1)


@pytest.fixture
async def partitions(sessionmaker: SM, test_settings: Settings) -> None:
    async with sessionmaker() as s:
        await run_maintenance(s, test_settings)


@pytest.fixture
async def shop(client: httpx.AsyncClient) -> dict[str, Any]:
    owner = await register(client, "Owner", "Shop Org")
    project = await create_project(client, owner, owner.org_id, "Shop")
    svc = {
        n: await create_service(client, owner, owner.org_id, project["id"], n)
        for n in ("order-service", "frontend", "old-service")
    }
    return {"owner": owner, "org": owner.org_id, "project": project, "svc": svc}


async def _usage(
    sm: SM, org: str, key: dict[str, Any], service_id: str, minute: datetime, **counts: int
) -> None:
    """counts: `<signal>__<outcome>` → count, outcome `accepted` or a rejection reason."""
    for name, count in counts.items():
        signal, outcome = name.split("__")
        await sql(
            sm,
            "INSERT INTO ingest_usage (org_id, key_id, service_id, signal, minute, outcome, count) "
            "VALUES (:o, :k, :s, :sig, :m, :out, :c)",
            o=org,
            k=key["id"],
            s=service_id,
            sig=signal,
            m=minute,
            out=outcome if outcome == "accepted" else f"rejected:{outcome}",
            c=count,
        )


async def test_overview_reports_stored_numbers(
    client: httpx.AsyncClient, sessionmaker: SM, shop: dict[str, Any], partitions: None
) -> None:
    org, owner = shop["org"], shop["owner"]
    order, frontend = shop["svc"]["order-service"]["id"], shop["svc"]["frontend"]["id"]
    now = datetime.now(UTC)
    t0 = floor_bucket(now) - timedelta(minutes=20)
    await traffic(sessionmaker, org, order, t0 - 120 * S, t0 + 180 * S, error=lambda t, i: t >= t0)
    await ticks(sessionmaker, t0, t0 + 90 * S, seen=[order])
    key = await create_api_key(client, owner, org, order)
    minute = now.replace(second=0, microsecond=0)
    await _usage(
        sessionmaker,
        org,
        key,
        order,
        minute - timedelta(minutes=2),
        traces__accepted=100,
        logs__timestamp=3,
    )
    await _usage(  # older than 15 min: not in the 15-min totals
        sessionmaker, org, key, order, minute - timedelta(minutes=30), metrics__accepted=50
    )
    old = shop["svc"]["old-service"]["id"]
    assert (
        await client.delete(f"/api/v1/orgs/{org}/services/{old}", headers=owner.headers)
    ).status_code in (200, 204)

    viewer = await register(client, "Vic", None)
    await add_member(client, owner, org, viewer, "viewer")
    resp = await client.get(
        f"/api/v1/orgs/{org}/overview",
        params={"project_id": shop["project"]["id"]},
        headers=viewer.headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["project_id"] == shop["project"]["id"]
    assert body["open_incidents"]["critical"] + body["open_incidents"]["warning"] == 1
    (latest,) = body["latest_incidents"]
    assert latest["number"] == 1
    assert latest["status"] == "open"
    rules = {r["rule"]: r["count"] for r in body["detections_24h"]}
    assert rules.get("error_rate", 0) >= 1
    by_name = {s["name"]: s for s in body["services"]}
    assert sorted(by_name) == ["frontend", "order-service"]  # archived service hidden
    assert body["services_truncated"] is False
    o = by_name["order-service"]
    assert o["ingest_accepted_15m"] == 100
    assert o["ingest_rejected_15m"] == 3
    assert sum(p["requests"] for p in o["series"]) > 0
    assert sum(p["errors"] for p in o["series"]) > 0
    assert all(p["errors"] <= p["requests"] for p in o["series"])
    minutes = [p["minute"] for p in o["series"]]
    assert minutes == sorted(minutes)
    f = by_name["frontend"]
    assert f["id"] == frontend
    assert f["series"] == []  # no rollups → no points, not zeros
    assert (f["ingest_accepted_15m"], f["ingest_rejected_15m"]) == (0, 0)


async def test_overview_of_an_empty_project_is_all_zero(
    client: httpx.AsyncClient, shop: dict[str, Any]
) -> None:
    owner = shop["owner"]
    empty = await create_project(client, owner, shop["org"], "Empty")
    body = (
        await client.get(
            f"/api/v1/orgs/{shop['org']}/overview",
            params={"project_id": empty["id"]},
            headers=owner.headers,
        )
    ).json()
    assert body["open_incidents"] == {"critical": 0, "warning": 0}
    assert body["latest_incidents"] == []
    assert body["detections_24h"] == []
    assert body["services"] == []


async def test_overview_requires_a_project_of_this_org(
    client: httpx.AsyncClient, shop: dict[str, Any]
) -> None:
    owner = shop["owner"]
    base = f"/api/v1/orgs/{shop['org']}/overview"
    missing = await client.get(base, headers=owner.headers)
    assert missing.status_code == 422
    eve = await register(client, "Eve", "Eve Org")
    foreign = await client.get(
        f"/api/v1/orgs/{eve.org_id}/overview",
        params={"project_id": shop["project"]["id"]},
        headers=eve.headers,
    )
    assert foreign.status_code == 404
    assert foreign.json()["error"]["code"] == "project_not_found"


async def test_ingest_usage_per_minute_paged_and_summed_over_keys(
    client: httpx.AsyncClient, sessionmaker: SM, shop: dict[str, Any]
) -> None:
    org, owner = shop["org"], shop["owner"]
    order = shop["svc"]["order-service"]["id"]
    k1 = await create_api_key(client, owner, org, order, "a")
    k2 = await create_api_key(client, owner, org, order, "b")
    m = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=5)
    await _usage(sessionmaker, org, k1, order, m, traces__accepted=10, logs__timestamp=2)
    await _usage(sessionmaker, org, k2, order, m, traces__accepted=5, traces__span_id=1)
    await _usage(sessionmaker, org, k1, order, m - timedelta(minutes=1), metrics__accepted=7)
    await _usage(sessionmaker, org, k1, order, m - timedelta(minutes=2), logs__accepted=1)
    await _usage(sessionmaker, org, k1, order, m - timedelta(hours=3), logs__accepted=99)
    url = f"/api/v1/orgs/{org}/services/{order}/ingest-usage"

    page1 = (await client.get(url, params={"limit": 2}, headers=owner.headers)).json()
    first, second = page1["items"]
    assert datetime.fromisoformat(first["minute"]) == m
    assert datetime.fromisoformat(second["minute"]) == m - timedelta(minutes=1)
    assert first["accepted"] == {"traces": 15, "logs": 0, "metrics": 0}
    assert first["rejected"] == {"traces": 1, "logs": 2, "metrics": 0}
    assert first["rejected_by_reason"] == {"timestamp": 2, "span_id": 1}
    assert second["accepted"]["metrics"] == 7
    assert page1["next_cursor"]
    page2 = (
        await client.get(
            url, params={"limit": 2, "cursor": page1["next_cursor"]}, headers=owner.headers
        )
    ).json()
    assert [i["accepted"]["logs"] for i in page2["items"]] == [1]  # 3 h ago is outside 1 h
    assert page2["next_cursor"] is None

    wide = await client.get(
        url,
        params={
            "from": (m - timedelta(hours=4)).isoformat(),
            "to": (m + timedelta(minutes=1)).isoformat(),
        },
        headers=owner.headers,
    )
    assert [i["accepted"]["logs"] for i in wide.json()["items"]][-1] == 99


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (timedelta(0), timedelta(0)),
        (timedelta(hours=1), timedelta(0)),
        (timedelta(0), timedelta(hours=25)),
    ],
)
async def test_ingest_usage_rejects_bad_ranges(
    client: httpx.AsyncClient, shop: dict[str, Any], start: timedelta, end: timedelta
) -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    order = shop["svc"]["order-service"]["id"]
    resp = await client.get(
        f"/api/v1/orgs/{shop['org']}/services/{order}/ingest-usage",
        params={"from": (base + start).isoformat(), "to": (base + end).isoformat()},
        headers=shop["owner"].headers,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_time_range"


async def test_ingest_usage_of_another_orgs_service_is_404(
    client: httpx.AsyncClient, sessionmaker: SM, shop: dict[str, Any]
) -> None:
    org, owner = shop["org"], shop["owner"]
    order = shop["svc"]["order-service"]["id"]
    key = await create_api_key(client, owner, org, order)
    await _usage(
        sessionmaker,
        org,
        key,
        order,
        datetime.now(UTC).replace(second=0, microsecond=0),
        traces__accepted=1,
    )
    eve = await register(client, "Eve", "Eve Org")
    resp = await client.get(
        f"/api/v1/orgs/{eve.org_id}/services/{order}/ingest-usage", headers=eve.headers
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "service_not_found"
