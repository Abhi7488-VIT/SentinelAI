"""Dependency graph (Phase 3 T6): observed-edge discovery job and the per-project graph view.

Edges come from evidence only (DB CHECK `evidence_required`):

- **declared** edges are written with the `service_dependencies` rows they cite
  (`ServiceRegistryService.set_dependencies`) and removed with them (FK cascade);
- **observed** edges are found here, every 60 s, from the last 2 minutes of spans:
  - service → service: a SERVER span of service B whose parent span belongs to service A, in the
    same trace (A calls B). Both must be active services of the same project;
  - service → external: a service's database CLIENT spans (`db.system` set), named
    `<db.system>:<db.namespace>` (falling back to `server.address`), e.g. `postgresql:orders`.

Telemetry is untrusted: external names are validated against the registry's external-target
pattern and capped per service, so a sender cannot grow the graph without bound. Every query is
anchored on one service and a bounded window, and the run holds an advisory lock (overlapping
runs skip) inside one transaction, so retries and concurrent runs are idempotent upserts.
"""

from __future__ import annotations

import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.tenancy import SystemScope, TenantContext
from app.models.graph import ServiceEdge
from app.models.project import Project
from app.models.service import EXTERNAL_TARGET_PATTERN, Service
from app.repositories.service_graph import (
    ServiceEdgeRepository,
    SpanGraphQueries,
    orgs_with_recent_telemetry,
    try_discovery_lock,
)
from app.repositories.services import ServiceRepository
from app.services.projects import ProjectService

logger = get_logger("app.graph")

DISCOVERY_WINDOW = timedelta(minutes=2)
# How long before a child span its parent may have started (a slow request's caller).
PARENT_LOOKBACK = timedelta(minutes=10)
MAX_CALLERS_PER_SERVICE = 50
MAX_DB_TARGETS_PER_SERVICE = 50
# Observed external targets kept per service; new names beyond this are skipped and counted.
MAX_OBSERVED_EXTERNALS_PER_SERVICE = 50
# Observed edges not seen for this long are deleted (declared edges never expire).
OBSERVED_EDGE_TTL = timedelta(days=7)
# Upper bound on edges returned for one project's graph (registry-sized; see `get_graph`).
MAX_GRAPH_EDGES = 5_000

_EXTERNAL_RE = re.compile(EXTERNAL_TARGET_PATTERN)


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    ran: bool
    orgs: int = 0
    services_scanned: int = 0
    service_edges: int = 0
    external_edges: int = 0
    expired: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    duration_ms: float = 0.0


def external_name(db_system: str, target: str) -> str | None:
    """`<db.system>:<target>` if it is a valid external-target name, else None."""
    if not db_system or not target:
        return None
    name = f"{db_system}:{target}"
    return name if _EXTERNAL_RE.fullmatch(name) else None


async def discover_edges(session: AsyncSession, *, now: datetime | None = None) -> DiscoveryResult:
    """One discovery run over all orgs with recent telemetry. Commits once at the end."""
    started = time.monotonic()
    now = now or datetime.now(UTC)
    if not await try_discovery_lock(session):
        await session.rollback()
        logger.info("graph_discovery_skipped", reason="locked")
        return DiscoveryResult(ran=False)

    window_start = now - DISCOVERY_WINDOW
    skipped: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    orgs = await orgs_with_recent_telemetry(session, window_start)
    for org_id in orgs:
        scope = SystemScope(org_id=org_id, actor="graph-discovery")
        await _discover_org(session, scope, window_start, now, totals, skipped)

    duration_ms = (time.monotonic() - started) * 1000
    await session.commit()
    result = DiscoveryResult(
        ran=True,
        orgs=len(orgs),
        services_scanned=totals["services"],
        service_edges=totals["service_edges"],
        external_edges=totals["external_edges"],
        expired=totals["expired"],
        skipped=dict(skipped),
        duration_ms=round(duration_ms, 1),
    )
    logger.info(
        "graph_discovery_done",
        orgs=result.orgs,
        services_scanned=result.services_scanned,
        service_edges=result.service_edges,
        external_edges=result.external_edges,
        expired=result.expired,
        skipped=result.skipped,
        duration_ms=result.duration_ms,
    )
    return result


async def _discover_org(
    session: AsyncSession,
    scope: SystemScope,
    start: datetime,
    end: datetime,
    totals: Counter[str],
    skipped: Counter[str],
) -> None:
    services = await ServiceRepository(session, scope).active()
    by_id = {s.id: s for s in services}
    spans = SpanGraphQueries(session, scope)
    edges = ServiceEdgeRepository(session, scope)

    for service in services:
        if service.last_seen_at is None or service.last_seen_at < start:
            continue
        totals["services"] += 1
        callers = await spans.callers_of(
            service.id,
            start=start,
            end=end,
            parent_from=start - PARENT_LOOKBACK,
            limit=MAX_CALLERS_PER_SERVICE,
        )
        for caller_id, call in callers:
            caller = by_id.get(caller_id)
            if caller is None:  # archived since, or not a registered active service
                skipped["caller_inactive"] += 1
                continue
            if caller.project_id != service.project_id:
                skipped["cross_project"] += 1
                continue
            await edges.upsert_observed(caller.id, target_service_id=service.id, call=call)
            totals["service_edges"] += 1

        await _discover_externals(spans, edges, service, start, end, totals, skipped)

    totals["expired"] += await edges.delete_observed_before(end - OBSERVED_EDGE_TTL)


async def _discover_externals(
    spans: SpanGraphQueries,
    edges: ServiceEdgeRepository,
    service: Service,
    start: datetime,
    end: datetime,
    totals: Counter[str],
    skipped: Counter[str],
) -> None:
    targets = await spans.database_targets(
        service.id, start=start, end=end, limit=MAX_DB_TARGETS_PER_SERVICE
    )
    if not targets:
        return
    known = await edges.observed_external_names(service.id)
    for db_system, target, call in targets:
        name = external_name(db_system, target)
        if name is None:
            skipped["invalid_external_name"] += 1
            continue
        if name not in known:
            if len(known) >= MAX_OBSERVED_EXTERNALS_PER_SERVICE:
                skipped["external_limit"] += 1
                continue
            known.add(name)
        await edges.upsert_observed(service.id, target_external=name, call=call)
        totals["external_edges"] += 1


# ---- read side ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GraphView:
    project: Project
    services: list[Service]
    edges: list[ServiceEdge]
    truncated: bool


class ServiceGraphService:
    def __init__(self, session: AsyncSession, ctx: TenantContext) -> None:
        self.session = session
        self.ctx = ctx

    async def get_graph(self, project_id: uuid.UUID) -> GraphView:
        """Active services of the project and every edge between them or to externals.

        Not paginated: a graph is only meaningful whole, and its size is bounded by the registry
        (services per project) and the per-service external cap; `MAX_GRAPH_EDGES` is a hard
        stop, reported as `truncated`.
        """
        project = await ProjectService(self.session, self.ctx).get(project_id)  # 404 if foreign
        services = sorted(
            await ServiceRepository(self.session, self.ctx).active_in_project(project.id),
            key=lambda s: (s.name, s.environment, s.id),
        )
        edges = await ServiceEdgeRepository(self.session, self.ctx).among_services(
            [s.id for s in services], limit=MAX_GRAPH_EDGES + 1
        )
        truncated = len(edges) > MAX_GRAPH_EDGES
        return GraphView(project, services, edges[:MAX_GRAPH_EDGES], truncated)
