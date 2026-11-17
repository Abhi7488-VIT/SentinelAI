"""Reusable trigger primitives. Each acts only on an allow-listed testbed target.

The controller has no Docker socket: traffic changes go through the load generator's control
API, network faults through Toxiproxy's API, schema changes through Postgres, and restarts and
deploys through each service's token-protected ops endpoint (harness.deploy).

Nothing here writes to the system's telemetry: the controller is not instrumented, so faults
show up only through their real effects (ADR 0003).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from harness import deploy as deploy_pipeline

SERVICES = deploy_pipeline.SERVICES
ALLOWED_MIXES = frozenset({"baseline", "with-weekly-reports", "cache-busting-client"})
ALLOWED_PROXIES = frozenset({"order-to-payment"})


@dataclass(frozen=True)
class SqlChangeDef:
    database: str
    apply: str
    revert: str


# Named schema changes; the controller never runs SQL that is not listed here.
SQL_CHANGES: dict[str, SqlChangeDef] = {
    # Both indexes the order-history query can use. Dropping only the composite one is not
    # enough (measured live, T8): the planner walks ix_orders_created_at backwards and, for the
    # load generator's users (who have many recent orders), finds 20 rows almost at once.
    "drop-order-history-indexes": SqlChangeDef(
        database="orders",
        apply=(
            "DROP INDEX IF EXISTS ix_orders_user_id_created_at; "
            "DROP INDEX IF EXISTS ix_orders_created_at"
        ),
        revert=(
            "CREATE INDEX IF NOT EXISTS ix_orders_user_id_created_at "
            "ON orders (user_id, created_at DESC); "
            "CREATE INDEX IF NOT EXISTS ix_orders_created_at ON orders (created_at)"
        ),
    ),
}


class NotAllowedError(ValueError):
    """A primitive was asked to act on a target outside its allow-list."""


@dataclass(frozen=True)
class ControllerSettings:
    loadgen_url: str = "http://loadgen:8089"
    toxiproxy_url: str = "http://toxiproxy:8474"
    postgres_host: str = "postgres"
    postgres_user: str = "testbed"
    postgres_password: str = ""
    ops_token: str = ""
    otlp_endpoint: str | None = None
    releases_file: Path = Path("/releases/releases.json")
    runs_dir: Path = Path("/runs")
    scenarios_dir: Path = Path("/app/scenarios")
    controller_token: str = ""
    min_available_mb: int = 500
    prometheus_url: str = "http://prometheus:9090"
    loki_url: str = "http://loki:3100"
    jaeger_url: str = "http://jaeger:16686"
    default_seed: int = 42

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ControllerSettings:
        e = dict(os.environ if env is None else env)
        d = cls()
        return cls(
            loadgen_url=e.get("LOADGEN_URL", d.loadgen_url),
            toxiproxy_url=e.get("TOXIPROXY_URL", d.toxiproxy_url),
            postgres_host=e.get("POSTGRES_HOST", d.postgres_host),
            postgres_user=e.get("POSTGRES_USER", d.postgres_user),
            postgres_password=e.get("POSTGRES_PASSWORD", ""),
            ops_token=e.get("OPS_TOKEN", ""),
            otlp_endpoint=e.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None,
            releases_file=Path(e.get("RELEASES_FILE", str(d.releases_file))),
            runs_dir=Path(e.get("RUNS_DIR", str(d.runs_dir))),
            scenarios_dir=Path(e.get("SCENARIOS_DIR", str(d.scenarios_dir))),
            controller_token=e.get("CONTROLLER_TOKEN", ""),
            min_available_mb=int(e.get("CONTROLLER_MIN_AVAILABLE_MB", str(d.min_available_mb))),
            prometheus_url=e.get("PROMETHEUS_URL", d.prometheus_url),
            loki_url=e.get("LOKI_URL", d.loki_url),
            jaeger_url=e.get("JAEGER_URL", d.jaeger_url),
            default_seed=int(e.get("SEED", str(d.default_seed))),
        )


@dataclass
class Context:
    """What a scenario plugin gets: settings, an HTTP client, the run's seed and a step log."""

    settings: ControllerSettings
    client: httpx.AsyncClient
    seed: int
    steps: list[dict[str, str]] = field(default_factory=list)
    on_step: Callable[[dict[str, str]], None] | None = None

    def step(self, name: str, detail: str) -> None:
        entry = {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "step": name,
            "detail": detail,
        }
        self.steps.append(entry)
        if self.on_step is not None:
            self.on_step(entry)


# ---- primitives ----------------------------------------------------------------------------


async def traffic_pattern(ctx: Context, mix: str) -> dict[str, Any]:
    """Switch the load generator to a named, seeded traffic mix (schedules restart at t=0)."""
    if mix not in ALLOWED_MIXES:
        raise NotAllowedError(f"traffic mix {mix!r} is not allow-listed")
    resp = await ctx.client.put(
        f"{ctx.settings.loadgen_url}/mix", json={"name": mix, "seed": ctx.seed}, timeout=10
    )
    resp.raise_for_status()
    body: dict[str, Any] = resp.json()
    ctx.step("traffic", f"mix={mix} seed={ctx.seed}")
    return body


async def sql_change(ctx: Context, name: str, *, revert: bool = False) -> None:
    """Apply (or revert) a named schema change as the Postgres superuser."""
    change = SQL_CHANGES.get(name)
    if change is None:
        raise NotAllowedError(f"SQL change {name!r} is not allow-listed")
    import asyncpg

    conn = await asyncpg.connect(
        host=ctx.settings.postgres_host,
        user=ctx.settings.postgres_user,
        password=ctx.settings.postgres_password,
        database=change.database,
        timeout=10,
    )
    try:
        await conn.execute(change.revert if revert else change.apply, timeout=300)
    finally:
        await conn.close()
    ctx.step("sql", f"{'revert' if revert else 'apply'} {name}")


async def network_link(ctx: Context, proxy: str, *, enabled: bool) -> None:
    """Enable or disable a Toxiproxy link (disabled = connection refused)."""
    if proxy not in ALLOWED_PROXIES:
        raise NotAllowedError(f"proxy {proxy!r} is not allow-listed")
    resp = await ctx.client.post(
        f"{ctx.settings.toxiproxy_url}/proxies/{proxy}", json={"enabled": enabled}, timeout=10
    )
    resp.raise_for_status()
    ctx.step("network", f"{proxy} enabled={enabled}")


async def restart_service(ctx: Context, service: str) -> int:
    """Ask a service to restart through its ops endpoint (Docker's policy starts it again)."""
    if service not in SERVICES:
        raise NotAllowedError(f"service {service!r} is not allow-listed")
    try:
        resp = await ctx.client.post(
            f"http://{service}:8000/ops/restart",
            headers={"X-Ops-Token": ctx.settings.ops_token},
            timeout=5,
        )
        status = resp.status_code
    except httpx.HTTPError as exc:  # already down (e.g. OOM-killed): the policy restarts it
        status = 0
        ctx.step("restart", f"{service}: not answering ({type(exc).__name__})")
        return status
    ctx.step("restart", f"{service}: HTTP {status}")
    return status


async def deploy(
    ctx: Context,
    service: str,
    version: str,
    config: dict[str, str] | None = None,
    *,
    wait_s: float = 45.0,
) -> dict[str, Any]:
    """Release a version through the testbed's deploy pipeline (emits deploy events)."""
    if service not in SERVICES:
        raise NotAllowedError(f"service {service!r} is not allow-listed")
    sink = deploy_pipeline.EventSink(
        ctx.settings.runs_dir / "deploy-events.jsonl", ctx.settings.otlp_endpoint
    )
    try:
        result = await deploy_pipeline.deploy(
            service,
            version,
            config=config,
            manifest_path=ctx.settings.releases_file,
            ops_token=ctx.settings.ops_token,
            sink=sink,
            wait_s=wait_s,
        )
    finally:
        sink.close()
    ctx.step("deploy", f"{service} {version}: {result.status} in {result.seconds}s")
    return {
        "deployment_id": result.deployment_id,
        "status": result.status,
        "previous_version": result.previous_version,
    }


def release_of(ctx: Context, service: str) -> tuple[str, dict[str, str]] | None:
    """The release the manifest pins for a service, or None (it runs its image's version)."""
    if service not in SERVICES:
        raise NotAllowedError(f"service {service!r} is not allow-listed")
    entry = deploy_pipeline.read_manifest(ctx.settings.releases_file)["services"].get(service)
    if not entry:
        return None
    return str(entry["version"]), {str(k): str(v) for k, v in entry.get("config", {}).items()}
