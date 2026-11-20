"""Flood the ingest endpoint and report status codes (local overload check, not a benchmark).

Usage (stack running; from backend/):
  uv run python -m scripts.ingest_overload --seconds 30 --concurrency 16 --spans 1000

Registers a throwaway user/org/project/service/key through the public API, then sends gzip'd
OTLP protobuf trace batches as fast as `concurrency` senders can, for `seconds`.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import gzip
import json
import secrets
import time

import httpx

from tests import otlp_factory as f


async def _setup(api: str) -> str:
    async with httpx.AsyncClient(base_url=api, timeout=10) as c:
        suffix = secrets.token_hex(4)
        reg = await c.post(
            "/api/v1/auth/register",
            json={
                "email": f"overload-{suffix}@example.com",
                "password": secrets.token_urlsafe(16),
                "display_name": "Overload",
                "org_name": f"Overload {suffix}",
            },
        )
        reg.raise_for_status()
        body = reg.json()
        h = {"Authorization": f"Bearer {body['access_token']}"}
        org = body["organization"]["id"]
        project = await c.post(f"/api/v1/orgs/{org}/projects", json={"name": "Flood"}, headers=h)
        project.raise_for_status()
        svc = await c.post(
            f"/api/v1/orgs/{org}/services",
            json={
                "project_id": project.json()["id"],
                "name": "flood-service",
                "environment": "dev",
            },
            headers=h,
        )
        svc.raise_for_status()
        key = await c.post(
            f"/api/v1/orgs/{org}/services/{svc.json()['id']}/api-keys",
            json={"name": "flood"},
            headers=h,
        )
        key.raise_for_status()
        return str(key.json()["key"])


async def _run(args: argparse.Namespace) -> dict[str, object]:
    token = await _setup(args.api)
    body = gzip.compress(
        f.traces_request("flood-service", [f.span() for _ in range(args.spans)]).SerializeToString()
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-protobuf",
        "Content-Encoding": "gzip",
    }
    codes: collections.Counter[int] = collections.Counter()
    errors: collections.Counter[str] = collections.Counter()
    deadline = time.monotonic() + args.seconds

    async def sender(client: httpx.AsyncClient) -> None:
        while time.monotonic() < deadline:
            try:
                resp = await client.post("/api/v1/otlp/v1/traces", content=body, headers=headers)
                codes[resp.status_code] += 1
            except httpx.HTTPError as exc:
                errors[type(exc).__name__] += 1

    started = time.monotonic()
    async with httpx.AsyncClient(base_url=args.ingest, timeout=30) as client:
        await asyncio.gather(*(sender(client) for _ in range(args.concurrency)))
    elapsed = time.monotonic() - started
    return {
        "seconds": round(elapsed, 1),
        "concurrency": args.concurrency,
        "spans_per_request": args.spans,
        "request_body_bytes": len(body),
        "status_codes": dict(sorted(codes.items())),
        "client_errors": dict(errors),
        "requests_per_second": round(sum(codes.values()) / elapsed, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--ingest", default="http://127.0.0.1:8001")
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--spans", type=int, default=1000)
    print(json.dumps(asyncio.run(_run(parser.parse_args())), indent=2))  # noqa: T201


if __name__ == "__main__":
    main()
