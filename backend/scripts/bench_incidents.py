"""Latency of `GET /incidents` over 50,000 incidents (local benchmark, plan §12).

Usage (stack running; from backend/):
  uv run python -m scripts.bench_incidents \
      --database-url postgresql+asyncpg://sentinel:...@127.0.0.1:5432/sentinel \
      --out ../docs/evidence/phase-3/bench-incidents.json

What it does:
1. Registers a throwaway user/org/project and 20 services through the public API.
2. Seeds N incidents (default 50,000) for that org only, with one set-based INSERT: 2 services
   each, statuses 80 % resolved / 10 % open / 10 % investigating, 30 % critical, one every 50 s
   back from now, titles from 4 templates. Then ANALYZE.
3. For each filter: 20 warm-up requests, then `--requests` (default 1,000) sequential requests
   over one keep-alive connection to the running backend; records wall time per request and
   requires every response to be 200.
4. Deletes the seeded incidents and archives the project (unless --keep).

Results are written as JSON with machine info, commit and settings. They describe this machine
at this moment (other containers keep running), not a general capacity claim.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import secrets
import statistics
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.ext.asyncio import create_async_engine

SERVICES = 20

SEED_INCIDENTS = text(
    """
    INSERT INTO incidents (id, org_id, project_id, number, title, status, severity, started_at,
                           detected_at, last_activity_at, resolved_at, created_at, updated_at)
    SELECT gen_random_uuid(), :org, :project, n,
           (ARRAY['Errors', 'Latency shift', 'Resource saturation',
                  'Errors and latency'])[1 + n % 4]
             || ' in svc-' || lpad(((n * 7) % 20)::text, 2, '0')
             || ', svc-' || lpad(((n * 7 + 1) % 20)::text, 2, '0'),
           CASE WHEN n % 10 = 0 THEN 'open' WHEN n % 10 = 1 THEN 'investigating'
                ELSE 'resolved' END,
           CASE WHEN n % 10 < 3 THEN 'critical' ELSE 'warning' END,
           ts, ts + interval '1 min', ts + interval '5 min',
           CASE WHEN n % 10 >= 2 THEN ts + interval '30 min' END,
           ts + interval '1 min', ts + interval '1 min'
    FROM generate_series(1, :n) AS n,
         LATERAL (SELECT now() - (:n - n) * interval '50 seconds' AS ts) AS t
    """
)

SEED_SERVICES = text(
    """
    INSERT INTO incident_services (org_id, incident_id, service_id)
    SELECT i.org_id, i.id, (:ids)[1 + (i.number * 7 + k) % 20]
    FROM incidents i CROSS JOIN (VALUES (0), (1)) AS v(k)
    WHERE i.org_id = :org
    """
).bindparams(bindparam("ids", type_=ARRAY(UUID(as_uuid=True))))


def _machine() -> dict[str, Any]:
    mem_kib = 0
    cpu = platform.processor()
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                mem_kib = int(line.split()[1])
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return {
        "cpus": os.cpu_count(),
        "cpu_model": cpu,
        "mem_gib": round(mem_kib / 1024 / 1024, 1),
        "kernel": platform.release(),
        "python": platform.python_version(),
    }


def _commit() -> dict[str, Any]:
    def git(*args: str) -> str:
        return subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()

    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


def _pct(samples: list[float], p: float) -> float:
    """Nearest-rank percentile."""
    ordered = sorted(samples)
    k = max(0, min(len(ordered) - 1, math.ceil(p / 100 * len(ordered)) - 1))
    return round(ordered[k], 2)


async def _setup(c: httpx.AsyncClient) -> tuple[dict[str, str], str, str, list[str]]:
    suffix = secrets.token_hex(4)
    reg = await c.post(
        "/api/v1/auth/register",
        json={
            "email": f"bench-{suffix}@example.com",
            "password": secrets.token_urlsafe(16),
            "display_name": "Bench",
            "org_name": f"Bench {suffix}",
        },
    )
    reg.raise_for_status()
    body = reg.json()
    h = {"Authorization": f"Bearer {body['access_token']}"}
    org = body["organization"]["id"]
    project = await c.post(f"/api/v1/orgs/{org}/projects", json={"name": "Bench"}, headers=h)
    project.raise_for_status()
    project_id = project.json()["id"]
    ids = []
    for i in range(SERVICES):
        svc = await c.post(
            f"/api/v1/orgs/{org}/services",
            json={"project_id": project_id, "name": f"svc-{i:02d}", "environment": "bench"},
            headers=h,
        )
        svc.raise_for_status()
        ids.append(svc.json()["id"])
    return h, org, project_id, ids


async def _measure(
    c: httpx.AsyncClient, url: str, params: dict[str, str], h: dict[str, str], n: int
) -> dict[str, Any]:
    first = await c.get(url, params=params, headers=h)
    first.raise_for_status()
    items = len(first.json()["items"])
    for _ in range(20):
        (await c.get(url, params=params, headers=h)).raise_for_status()
    samples: list[float] = []
    errors = 0
    for _ in range(n):
        t0 = time.perf_counter()
        resp = await c.get(url, params=params, headers=h)
        samples.append((time.perf_counter() - t0) * 1000)
        errors += resp.status_code != 200
    return {
        "params": params,
        "items_per_page": items,
        "requests": n,
        "non_200": errors,
        "p50_ms": _pct(samples, 50),
        "p95_ms": _pct(samples, 95),
        "p99_ms": _pct(samples, 99),
        "mean_ms": round(statistics.fmean(samples), 2),
        "max_ms": round(max(samples), 2),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--incidents", type=int, default=50_000)
    parser.add_argument("--requests", type=int, default=1_000)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--keep", action="store_true", help="keep the seeded incidents")
    args = parser.parse_args()

    engine = create_async_engine(args.database_url)
    async with httpx.AsyncClient(base_url=args.api, timeout=30) as c:
        h, org, project_id, service_ids = await _setup(c)
        t0 = time.perf_counter()
        async with engine.begin() as conn:
            await conn.execute(
                SEED_INCIDENTS, {"org": org, "project": project_id, "n": args.incidents}
            )
            await conn.execute(
                SEED_SERVICES, {"org": org, "ids": [uuid.UUID(i) for i in service_ids]}
            )
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text("ANALYZE incidents"))
            await conn.execute(text("ANALYZE incident_services"))
            rows = await conn.execute(
                text("SELECT status, count(*) FROM incidents WHERE org_id = :o GROUP BY 1"),
                {"o": org},
            )
            counts: dict[str, int] = {str(s): int(n) for s, n in rows.all()}
        seed_s = round(time.perf_counter() - t0, 1)

        url = f"/api/v1/orgs/{org}/incidents"
        day_end = datetime.now(UTC) - timedelta(days=10)
        filters: dict[str, dict[str, str]] = {
            "no_filter": {},
            "status_open": {"status": "open"},
            "severity_critical": {"severity": "critical"},
            "status_resolved_severity_critical": {"status": "resolved", "severity": "critical"},
            "project": {"project_id": project_id},
            "service": {"service_id": service_ids[7]},
            "time_range_1_day": {
                "from": (day_end - timedelta(days=1)).isoformat(),
                "to": day_end.isoformat(),
            },
            "search_title": {"q": "Latency shift in svc-07"},  # matches 1 in 20 incidents
            "search_title_no_match": {"q": "saturation in svc-07"},  # matches none
            "search_number": {"q": "#25000"},
        }
        results = {}
        for name, params in filters.items():
            results[name] = await _measure(c, url, params, h, args.requests)
            r = results[name]
            print(  # noqa: T201
                f"{name:36} p50 {r['p50_ms']:7.2f}  p95 {r['p95_ms']:7.2f}  "
                f"p99 {r['p99_ms']:7.2f} ms  items {r['items_per_page']}  non-200 {r['non_200']}"
            )

        if not args.keep:
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM incidents WHERE org_id = :o"), {"o": org})
            (
                await c.delete(f"/api/v1/orgs/{org}/projects/{project_id}", headers=h)
            ).raise_for_status()
    await engine.dispose()

    report = {
        "label": "measured on this machine; other containers (testbed, ingest) were running",
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        **_commit(),
        "machine": _machine(),
        "target": {
            "api": args.api,
            "server": "backend container: one uvicorn process, DB pool from DB_POOL_SIZE",
            "client": "sequential, one keep-alive connection, 20 warm-up requests per filter",
            "page_size": 50,
        },
        "dataset": {
            "incidents": args.incidents,
            "by_status": counts,
            "services": SERVICES,
            "services_per_incident": 2,
            "seed_seconds": seed_s,
            "kept": args.keep,
        },
        "results": results,
    }
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"written: {args.out}")  # noqa: T201


if __name__ == "__main__":
    asyncio.run(main())
