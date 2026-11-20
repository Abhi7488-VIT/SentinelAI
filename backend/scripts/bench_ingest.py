"""Sustained OTLP ingest throughput until rows are written (local benchmark, plan §12).

Usage (stack running; from backend/):
  uv run python -m scripts.bench_ingest \
      --database-url postgresql+asyncpg://sentinel:...@127.0.0.1:5432/sentinel \
      --out ../docs/evidence/phase-3/bench/ingest.json

For each sender count (default 1, 4, 8):
- every sender gets its own new service and ingest key (the per-key limits, 50 requests/s and
  20,000 records/s, then cap each sender, not the whole run);
- senders POST protobuf batches (identity encoding) for `--seconds`, alternating 1,000 spans and
  1,000 log records, every record with fresh ids; 429/503 are honoured via Retry-After and counted;
- after the last send, Postgres is polled until the rows of those services equal the accepted
  record count. Sustained rate = accepted records / (time the last row was visible - start).

Then a malformed check: one undecodable body (expect 400) and one batch with 3 invalid spans of
10 (expect 200 + partial_success rejected 3), read back through the public ingest-usage API and
the ingest process's `ingest_records_total{outcome="rejected"}` counter.

Results are JSON with machine info and commit: this machine at this moment (the testbed keeps
sending its own telemetry through the same writer), not a general capacity claim.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from scripts.bench_incidents import _commit, _machine
from tests import otlp_factory as f

BATCH = 1_000
PROTOBUF = "application/x-protobuf"

COUNT_ROWS = text(
    """
    SELECT (SELECT count(*) FROM spans WHERE org_id = :org AND service_id = ANY(:ids))
         + (SELECT count(*) FROM log_records WHERE org_id = :org AND service_id = ANY(:ids))
    """
).bindparams(bindparam("ids", type_=ARRAY(UUID(as_uuid=True))))


class Api:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.c = client
        self.h: dict[str, str] = {}
        self.org = ""
        self.project = ""
        self.email = ""
        self.password = ""

    async def setup(self) -> None:
        suffix = secrets.token_hex(4)
        self.email = f"bench-ingest-{suffix}@example.com"
        self.password = secrets.token_urlsafe(16)
        reg = await self.c.post(
            "/api/v1/auth/register",
            json={
                "email": self.email,
                "password": self.password,
                "display_name": "Bench",
                "org_name": f"Bench ingest {suffix}",
            },
        )
        reg.raise_for_status()
        body = reg.json()
        self.h = {"Authorization": f"Bearer {body['access_token']}"}
        self.org = body["organization"]["id"]
        p = await self.c.post(
            f"/api/v1/orgs/{self.org}/projects", json={"name": "Bench"}, headers=self.h
        )
        p.raise_for_status()
        self.project = p.json()["id"]

    async def login(self) -> None:
        """Fresh access token (they live 15 min; a run can take longer)."""
        resp = await self.c.post(
            "/api/v1/auth/login", json={"email": self.email, "password": self.password}
        )
        resp.raise_for_status()
        self.h = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def service_with_key(self, name: str) -> tuple[str, str]:
        s = await self.c.post(
            f"/api/v1/orgs/{self.org}/services",
            json={"project_id": self.project, "name": name, "environment": "bench"},
            headers=self.h,
        )
        s.raise_for_status()
        sid = s.json()["id"]
        k = await self.c.post(
            f"/api/v1/orgs/{self.org}/services/{sid}/api-keys",
            json={"name": "bench"},
            headers=self.h,
        )
        k.raise_for_status()
        return sid, str(k.json()["key"])


def _traces(service: str, n: int, bad: int = 0) -> bytes:
    start = f.now_ns()
    spans = [f.span(start_ns=start) for _ in range(n)]
    for s in spans[:bad]:
        s.trace_id = b"\x01\x02\x03"  # invalid length → rejected per record
    return f.traces_request(service, spans).SerializeToString()


def _logs(service: str, n: int) -> bytes:
    # Identical log records are one record by design (dedup on a content hash + timestamp), so
    # every record gets its own body.
    ts = f.now_ns()
    tag = secrets.token_hex(8)
    records = [f.log_record(f"request {tag}-{i} handled", ts_ns=ts + i) for i in range(n)]
    return f.logs_request(service, records).SerializeToString()


async def _sender(
    ingest: httpx.AsyncClient,
    service: str,
    key: str,
    deadline: float,
    stats: SenderStats,
) -> None:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": PROTOBUF}
    i = 0
    while time.monotonic() < deadline:
        signal = "traces" if i % 2 == 0 else "logs"
        i += 1
        t_build = time.perf_counter()
        body = _traces(service, BATCH) if signal == "traces" else _logs(service, BATCH)
        stats.build_s += time.perf_counter() - t_build
        try:
            resp = await ingest.post(f"/api/v1/otlp/v1/{signal}", content=body, headers=headers)
        except httpx.HTTPError as exc:
            stats.client_errors[type(exc).__name__] += 1
            continue
        stats.codes[resp.status_code] += 1
        if resp.status_code == 200:
            stats.accepted[signal] += BATCH  # every record is valid; checked below via rows
        elif resp.status_code in (429, 503):
            await asyncio.sleep(min(float(resp.headers.get("Retry-After", "1")), 5.0))


async def _rows(engine: AsyncEngine, org: str, ids: list[str]) -> int:
    async with engine.connect() as conn:
        n = await conn.scalar(COUNT_ROWS, {"org": org, "ids": [uuid.UUID(i) for i in ids]})
    return int(n or 0)


@dataclass
class SenderStats:
    codes: collections.Counter[int] = field(default_factory=collections.Counter)
    accepted: collections.Counter[str] = field(default_factory=collections.Counter)
    client_errors: collections.Counter[str] = field(default_factory=collections.Counter)
    build_s: float = 0.0


async def _level(
    api: Api, ingest: httpx.AsyncClient, engine: AsyncEngine, senders: int, seconds: float
) -> dict[str, Any]:
    services = [await api.service_with_key(f"bench-c{senders}-{i}") for i in range(senders)]
    names = [f"bench-c{senders}-{i}" for i in range(senders)]
    stats = [SenderStats() for _ in range(senders)]
    t0 = time.monotonic()
    deadline = t0 + seconds
    await asyncio.gather(
        *(_sender(ingest, names[i], services[i][1], deadline, stats[i]) for i in range(senders))
    )
    send_s = time.monotonic() - t0
    accepted = sum(sum(s.accepted.values()) for s in stats)
    ids = [sid for sid, _ in services]
    rows, last_growth, last_rows = 0, time.monotonic(), -1
    while time.monotonic() - t0 < seconds + 300:
        rows = await _rows(engine, api.org, ids)
        if rows >= accepted:
            break
        if rows != last_rows:
            last_rows, last_growth = rows, time.monotonic()
        elif time.monotonic() - last_growth > 30:
            break  # stalled: reported as all_rows_written = false
        await asyncio.sleep(0.25)
    written_s = time.monotonic() - t0
    codes: collections.Counter[int] = collections.Counter()
    by_signal: collections.Counter[str] = collections.Counter()
    errors: collections.Counter[str] = collections.Counter()
    for s in stats:
        codes.update(s.codes)
        by_signal.update(s.accepted)
        errors.update(s.client_errors)
    return {
        "senders": senders,
        "send_seconds": round(send_s, 1),
        "status_codes": {str(k): v for k, v in sorted(codes.items())},
        "client_errors": dict(errors),
        "accepted_records": accepted,
        "accepted_by_signal": dict(by_signal),
        "rows_written": rows,
        "all_rows_written": rows >= accepted,
        "seconds_until_last_row": round(written_s, 1),
        "sustained_records_per_s": round(rows / written_s),
        "sustained_spans_per_s": round(by_signal["traces"] / written_s)
        if rows >= accepted
        else None,
        "client_build_seconds": round(sum(s.build_s for s in stats), 1),
    }


def _partial(resp: httpx.Response) -> dict[str, Any]:
    """OTLP answers a protobuf request in protobuf: decode its partial_success."""
    if resp.headers.get("content-type", "").startswith(PROTOBUF):
        msg = ExportTraceServiceResponse.FromString(resp.content)
        return {
            "partial_success": {
                "rejected_spans": msg.partial_success.rejected_spans,
                "error_message": msg.partial_success.error_message,
            }
        }
    body: dict[str, Any] = resp.json()
    return body


def _rejected_counter(metrics_text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in metrics_text.splitlines():
        m = re.match(r"ingest_records_total\{(.*)\} ([0-9.e+]+)$", line)
        if m and 'outcome="rejected"' in m.group(1):
            out[m.group(1)] = float(m.group(2))
    return out


async def _malformed(api: Api, ingest: httpx.AsyncClient) -> dict[str, Any]:
    sid, key = await api.service_with_key("bench-malformed")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": PROTOBUF}
    before = _rejected_counter((await ingest.get("/metrics")).text)
    garbage = await ingest.post(
        "/api/v1/otlp/v1/traces", content=os.urandom(64) + b"\xff\xff", headers=headers
    )
    partial = await ingest.post(
        "/api/v1/otlp/v1/traces", content=_traces("bench-malformed", 10, bad=3), headers=headers
    )
    after = _rejected_counter((await ingest.get("/metrics")).text)
    usage: list[Any] = []
    for _ in range(60):  # the writer records usage asynchronously
        resp = await api.c.get(f"/api/v1/orgs/{api.org}/services/{sid}/ingest-usage", headers=api.h)
        resp.raise_for_status()
        usage = resp.json()["items"]
        rejected = sum(sum(m["rejected"].values()) for m in usage)
        if rejected >= 3 and any(m["accepted"]["traces"] for m in usage):
            break
        await asyncio.sleep(1)
    delta = {k: after[k] - before.get(k, 0.0) for k in after if after[k] != before.get(k, 0.0)}
    return {
        "undecodable_body": {"status": garbage.status_code, "body": garbage.json()},
        "three_invalid_of_ten": {"status": partial.status_code, "body": _partial(partial)},
        "ingest_records_total_rejected_delta": delta,
        "ingest_usage_api": usage,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--ingest", default="http://127.0.0.1:8001")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--senders", default="1,4,8")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    engine = create_async_engine(args.database_url)
    async with (
        httpx.AsyncClient(base_url=args.api, timeout=30) as client,
        httpx.AsyncClient(
            base_url=args.ingest, timeout=30, limits=httpx.Limits(max_connections=32)
        ) as ingest,
    ):
        api = Api(client)
        await api.setup()
        levels = []
        for n in (int(x) for x in args.senders.split(",")):
            await api.login()
            r = await _level(api, ingest, engine, n, args.seconds)
            levels.append(r)
            print(  # noqa: T201
                f"senders {n}: accepted {r['accepted_records']:,} rows {r['rows_written']:,} "
                f"in {r['seconds_until_last_row']} s -> {r['sustained_records_per_s']:,} records/s "
                f"(spans/s {r['sustained_spans_per_s']}) codes {r['status_codes']}"
            )
            await asyncio.sleep(10)  # let the queue drain before the next level
        await api.login()
        malformed = await _malformed(api, ingest)
        print(json.dumps(malformed, indent=2)[:1500])  # noqa: T201
        await api.login()
        (
            await client.delete(f"/api/v1/orgs/{api.org}/projects/{api.project}", headers=api.h)
        ).raise_for_status()
    await engine.dispose()

    report = {
        "label": "measured on this machine; the testbed kept sending its own telemetry",
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        **_commit(),
        "machine": _machine(),
        "setup": {
            "batch_records": BATCH,
            "encoding": "protobuf, identity (no gzip)",
            "signals": "alternating traces and logs batches",
            "keys": "one service + key per sender",
            "seconds_per_level": args.seconds,
            "writer": "ingest-writer container (Celery, ingest queue)",
        },
        "levels": levels,
        "malformed": malformed,
    }
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(f"written: {args.out}")  # noqa: T201


if __name__ == "__main__":
    asyncio.run(main())
