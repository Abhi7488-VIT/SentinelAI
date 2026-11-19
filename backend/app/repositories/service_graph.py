"""Dependency graph queries: edge rows, and the span queries that discover observed edges.

Tenant-scoped like every repository (`org_id` from the scope). The one cross-tenant query,
`orgs_with_recent_telemetry`, returns only org ids for the background job, which then works
through a `SystemScope` per org.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, LargeBinary, and_, delete, func, literal_column, select, text
from sqlalchemy.dialects.postgresql import ARRAY, aggregate_order_by, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import SQLCoreOperations

from app.models.graph import EdgeSource, ServiceEdge
from app.models.service import Service, ServiceDependency
from app.models.telemetry import Span, SpanKind
from app.repositories.base import TenantRepository

# Arbitrary constant: the edge-discovery job's transaction-scoped advisory lock id.
DISCOVERY_LOCK_ID = 0x5E471_0002

# Literal (not bound) so the planner can match the partial indexes' predicates.
_KIND_SERVER: ColumnElement[int] = literal_column(str(int(SpanKind.SERVER)))
_KIND_CLIENT: ColumnElement[int] = literal_column(str(int(SpanKind.CLIENT)))


@dataclass(frozen=True, slots=True)
class ObservedCall:
    """Calls aggregated over one window: how often, when, and one trace as proof."""

    calls: int
    first_at: datetime
    last_at: datetime
    sample_trace_id: bytes


async def try_discovery_lock(session: AsyncSession) -> bool:
    return bool(
        await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(:id)"), {"id": DISCOVERY_LOCK_ID}
        )
    )


async def orgs_with_recent_telemetry(session: AsyncSession, since: datetime) -> list[uuid.UUID]:
    """System query: orgs that have an active service which sent telemetry since `since`."""
    stmt = (
        select(Service.org_id)
        .where(Service.archived_at.is_(None), Service.last_seen_at >= since)
        .distinct()
        .order_by(Service.org_id)
    )
    return list((await session.scalars(stmt)).all())


def _latest_trace(
    trace_col: SQLCoreOperations[bytes], time_col: SQLCoreOperations[datetime]
) -> ColumnElement[bytes]:
    """The trace id of the most recent span in the group (one sample, as evidence)."""
    agg = func.array_agg(aggregate_order_by(trace_col, time_col.desc()), type_=ARRAY(LargeBinary))
    return agg[1]  # Postgres arrays are 1-based


class SpanGraphQueries(TenantRepository[Span]):
    """Read-only span aggregations for edge discovery. Both are anchored on one service and a
    bounded time window, and use the per-partition indexes listed in the Phase 3 plan §3.2."""

    model = Span

    async def callers_of(
        self,
        service_id: uuid.UUID,
        *,
        start: datetime,
        end: datetime,
        parent_from: datetime,
        limit: int,
    ) -> list[tuple[uuid.UUID, ObservedCall]]:
        """Services whose span is the parent of one of `service_id`'s SERVER spans in
        [start, end): the caller → callee edges into this service, with a sample trace."""
        org = self.ctx.org_id
        child = aliased(Span, name="c")
        parent = aliased(Span, name="p")
        stmt = (
            select(
                parent.service_id,
                func.count(),
                func.min(child.start_time),
                func.max(child.start_time),
                _latest_trace(child.trace_id, child.start_time),
            )
            .select_from(child)
            .join(
                parent,
                and_(
                    parent.org_id == org,
                    parent.trace_id == child.trace_id,
                    parent.span_id == child.parent_span_id,
                    # A parent starts before its child; bounding it prunes partitions.
                    parent.start_time >= parent_from,
                    parent.start_time < end,
                ),
            )
            .where(
                child.org_id == org,
                child.service_id == service_id,
                child.kind == _KIND_SERVER,
                child.start_time >= start,
                child.start_time < end,
                child.parent_span_id.is_not(None),
                parent.service_id != service_id,
            )
            .group_by(parent.service_id)
            .order_by(func.count().desc(), parent.service_id)
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [(r[0], ObservedCall(int(r[1]), r[2], r[3], bytes(r[4]))) for r in rows]

    async def database_targets(
        self, service_id: uuid.UUID, *, start: datetime, end: datetime, limit: int
    ) -> list[tuple[str, str, ObservedCall]]:
        """(db_system, namespace-or-server-address, calls) for the service's DB CLIENT spans.

        The values are raw telemetry (untrusted); the caller validates them before use.
        """
        target = func.coalesce(Span.attributes["db.namespace"].astext, Span.server_address)
        stmt = (
            select(
                Span.db_system,
                target,
                func.count(),
                func.min(Span.start_time),
                func.max(Span.start_time),
                _latest_trace(Span.trace_id, Span.start_time),
            )
            .where(
                Span.org_id == self.ctx.org_id,
                Span.service_id == service_id,
                Span.kind == _KIND_CLIENT,
                Span.db_system.is_not(None),
                Span.start_time >= start,
                Span.start_time < end,
            )
            .group_by(Span.db_system, target)
            .order_by(func.count().desc(), Span.db_system, target)
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            (str(r[0]), str(r[1] or ""), ObservedCall(int(r[2]), r[3], r[4], bytes(r[5])))
            for r in rows
        ]


class ServiceEdgeRepository(TenantRepository[ServiceEdge]):
    model = ServiceEdge

    async def add_declared(self, deps: Sequence[ServiceDependency], *, at: datetime) -> None:
        """One declared edge per dependency row (the row is the edge's evidence)."""
        for dep in deps:
            await self.add(
                ServiceEdge(
                    source_service_id=dep.service_id,
                    target_service_id=dep.target_service_id,
                    target_external=dep.target_external,
                    source=EdgeSource.DECLARED.value,
                    first_seen_at=at,
                    last_seen_at=at,
                    call_count=0,
                    declared_dependency_id=dep.id,
                )
            )

    async def upsert_observed(
        self,
        source_service_id: uuid.UUID,
        *,
        target_service_id: uuid.UUID | None = None,
        target_external: str | None = None,
        call: ObservedCall,
    ) -> None:
        if (target_service_id is None) == (target_external is None):
            raise ValueError("exactly one of target_service_id / target_external")
        stmt = insert(ServiceEdge).values(
            org_id=self.ctx.org_id,
            source_service_id=source_service_id,
            target_service_id=target_service_id,
            target_external=target_external,
            source=EdgeSource.OBSERVED.value,
            first_seen_at=call.first_at,
            last_seen_at=call.last_at,
            call_count=call.calls,
            sample_trace_id=call.sample_trace_id,
        )
        target_col = "target_service_id" if target_service_id is not None else "target_external"
        stmt = stmt.on_conflict_do_update(
            index_elements=["org_id", "source_service_id", target_col, "source"],
            index_where=text(f"{target_col} IS NOT NULL"),
            set_={
                "first_seen_at": func.least(ServiceEdge.first_seen_at, stmt.excluded.first_seen_at),
                "last_seen_at": func.greatest(ServiceEdge.last_seen_at, stmt.excluded.last_seen_at),
                "call_count": stmt.excluded.call_count,
                "sample_trace_id": stmt.excluded.sample_trace_id,
            },
        )
        await self.session.execute(stmt)

    async def observed_external_names(self, source_service_id: uuid.UUID) -> set[str]:
        stmt = select(ServiceEdge.target_external).where(
            ServiceEdge.org_id == self.ctx.org_id,
            ServiceEdge.source_service_id == source_service_id,
            ServiceEdge.source == EdgeSource.OBSERVED.value,
            ServiceEdge.target_external.is_not(None),
        )
        return {name for name in (await self.session.scalars(stmt)).all() if name is not None}

    async def delete_observed_before(self, cutoff: datetime) -> int:
        result = await self.session.execute(
            delete(ServiceEdge).where(
                ServiceEdge.org_id == self.ctx.org_id,
                ServiceEdge.source == EdgeSource.OBSERVED.value,
                ServiceEdge.last_seen_at < cutoff,
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def among_services(
        self, service_ids: Sequence[uuid.UUID], *, limit: int
    ) -> list[ServiceEdge]:
        """Edges whose source is one of `service_ids` and whose target is one of them or an
        external system. Deterministic order."""
        if not service_ids:
            return []
        stmt = (
            self.scoped()
            .where(
                ServiceEdge.source_service_id.in_(service_ids),
                (ServiceEdge.target_service_id.is_(None))
                | ServiceEdge.target_service_id.in_(service_ids),
            )
            .order_by(
                ServiceEdge.source_service_id,
                ServiceEdge.target_service_id.is_(None),
                ServiceEdge.target_service_id,
                ServiceEdge.target_external,
                ServiceEdge.source,
            )
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())
