"""Read-only telemetry queries for investigation tools (Phase 4 plan §4).

Every query is tenant-scoped (`org_id` from the scope), anchored on explicit service ids and a
bounded time window (partition pruning + the per-partition indexes), and row-limited. Aggregation
happens in SQL; Python only receives bounded, reduced rows. No query here writes anything.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, text

from app.models.graph import ServiceEdge
from app.models.telemetry import LogRecord, MetricKind, MetricSeries, Span
from app.repositories.base import TenantRepository

METRIC_GAUGE = int(MetricKind.GAUGE)
METRIC_SUM = int(MetricKind.SUM)
METRIC_HISTOGRAM = int(MetricKind.HISTOGRAM)

# Default per-statement ceiling for tool queries (the connection default may be raised).
TOOL_STATEMENT_TIMEOUT_MS = 5000


async def bound_statement_time(
    repo: TenantRepository[Any], ms: int = TOOL_STATEMENT_TIMEOUT_MS
) -> None:
    """Cap every following statement of this transaction at `ms` (SET LOCAL)."""
    await repo.session.execute(text(f"SET LOCAL statement_timeout = {int(ms)}"))


@dataclass(frozen=True, slots=True)
class Window:
    start: datetime
    end: datetime


# --- logs -------------------------------------------------------------------------------------


class LogEvidenceQueries(TenantRepository[LogRecord]):
    model = LogRecord

    async def count(self, service_id: uuid.UUID, w: Window, min_severity: int) -> int:
        return int(
            await self.session.scalar(
                text(
                    "SELECT count(*) FROM log_records WHERE org_id = :org AND service_id = :svc"
                    " AND ts >= :s AND ts < :e AND severity_number >= :sev"
                ),
                {
                    "org": self.ctx.org_id,
                    "svc": service_id,
                    "s": w.start,
                    "e": w.end,
                    "sev": min_severity,
                },
            )
            or 0
        )

    async def samples(
        self, service_id: uuid.UUID, w: Window, min_severity: int, limit: int
    ) -> list[dict[str, Any]]:
        """Most recent `limit` records at or above `min_severity`, with capped text fields."""
        rows = await self.session.execute(
            text(
                "SELECT encode(record_hash, 'hex') AS record_id, ts, severity_number,"
                "  severity_text,"
                "  left(body, 2000) AS body, left(exception_type, 256) AS exception_type,"
                "  left(exception_message, 1000) AS exception_message,"
                "  encode(trace_id, 'hex') AS trace_id, attributes"
                " FROM log_records WHERE org_id = :org AND service_id = :svc"
                "  AND ts >= :s AND ts < :e AND severity_number >= :sev"
                " ORDER BY ts DESC, record_hash LIMIT :lim"
            ),
            {
                "org": self.ctx.org_id,
                "svc": service_id,
                "s": w.start,
                "e": w.end,
                "sev": min_severity,
                "lim": limit,
            },
        )
        return [dict(r._mapping) for r in rows]

    async def exception_signatures(
        self, service_ids: Sequence[uuid.UUID], w: Window, limit: int
    ) -> list[dict[str, Any]]:
        """(service, exception type, message, one stack trace, count) from logs."""
        rows = await self.session.execute(
            text(
                "SELECT service_id, left(exception_type, 256) AS exception_type,"
                "  left(exception_message, 1000) AS exception_message, count(*) AS n,"
                "  (array_agg(left(exception_stacktrace, 8000) ORDER BY ts DESC)"
                "     FILTER (WHERE exception_stacktrace IS NOT NULL))[1] AS stacktrace"
                " FROM log_records WHERE org_id = :org AND service_id = ANY(:svcs)"
                "  AND ts >= :s AND ts < :e AND exception_type IS NOT NULL"
                " GROUP BY 1, 2, 3 ORDER BY n DESC, 2, 3 LIMIT :lim"
            ),
            {
                "org": self.ctx.org_id,
                "svcs": list(service_ids),
                "s": w.start,
                "e": w.end,
                "lim": limit,
            },
        )
        return [dict(r._mapping) for r in rows]


# --- metrics ----------------------------------------------------------------------------------

_METRIC_DELTAS_SQL = text(
    """
    WITH s AS (
        SELECT id, name, unit, kind, is_monotonic, temporality, attributes
        FROM metric_series
        WHERE org_id = :org AND service_id = :svc
          AND (CAST(:names AS text[]) IS NULL OR name = ANY(CAST(:names AS text[])))
        ORDER BY name, id
        LIMIT :max_series
    ),
    p AS (
        SELECT p.series_id, p.ts, p.start_ts, p.count, p.sum,
               CASE WHEN s.kind = :k_hist THEN NULL ELSE p.value END AS v,
               (p.ts >= :ws) AS during
        FROM metric_points p JOIN s ON s.id = p.series_id
        WHERE p.org_id = :org AND p.ts >= :bs AND p.ts < :we
          AND (p.ts < :be OR p.ts >= :ws)
    ),
    agg AS (
        SELECT series_id, during,
               count(*) AS n, avg(v) AS mean, stddev_pop(v) AS sd,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY v) AS p95, max(v) AS mx,
               (array_agg(v ORDER BY ts))[1] AS first_v,
               (array_agg(v ORDER BY ts DESC))[1] AS last_v,
               extract(epoch FROM max(ts) - min(ts)) AS span_s,
               count(DISTINCT start_ts) AS starts,
               sum(sum) AS h_sum, sum(count) AS h_count,
               (array_agg(sum ORDER BY ts))[1] AS h_first_sum,
               (array_agg(sum ORDER BY ts DESC))[1] AS h_last_sum,
               (array_agg(count ORDER BY ts))[1] AS h_first_count,
               (array_agg(count ORDER BY ts DESC))[1] AS h_last_count
        FROM p GROUP BY series_id, during
    ),
    onset AS (
        SELECT p.series_id, min(p.ts) AS onset
        FROM p JOIN agg b ON b.series_id = p.series_id AND NOT b.during
        JOIN s ON s.id = p.series_id
        WHERE p.during AND p.v IS NOT NULL AND b.mean IS NOT NULL
          AND s.kind IN (:k_gauge, :k_sum) AND NOT s.is_monotonic
          AND abs(p.v - b.mean) > greatest(3 * coalesce(b.sd, 0), 0.1 * abs(b.mean), 1e-9)
        GROUP BY p.series_id
    )
    SELECT s.id, s.name, s.unit, s.kind, s.is_monotonic, s.temporality, s.attributes,
           b.n AS b_n, b.mean AS b_mean, b.p95 AS b_p95, b.mx AS b_max, b.first_v AS b_first,
           b.last_v AS b_last, b.span_s AS b_span, b.starts AS b_starts,
           b.h_sum AS b_hsum, b.h_count AS b_hcount, b.h_first_sum AS b_hfs, b.h_last_sum AS b_hls,
           b.h_first_count AS b_hfc, b.h_last_count AS b_hlc,
           d.n AS d_n, d.mean AS d_mean, d.p95 AS d_p95, d.mx AS d_max, d.first_v AS d_first,
           d.last_v AS d_last, d.span_s AS d_span, d.starts AS d_starts,
           d.h_sum AS d_hsum, d.h_count AS d_hcount, d.h_first_sum AS d_hfs, d.h_last_sum AS d_hls,
           d.h_first_count AS d_hfc, d.h_last_count AS d_hlc,
           o.onset
    FROM s
    LEFT JOIN agg b ON b.series_id = s.id AND NOT b.during
    LEFT JOIN agg d ON d.series_id = s.id AND d.during
    LEFT JOIN onset o ON o.series_id = s.id
    WHERE b.n IS NOT NULL OR d.n IS NOT NULL
    ORDER BY s.name, s.id
    """
)


class MetricEvidenceQueries(TenantRepository[MetricSeries]):
    model = MetricSeries

    async def series_count(self, service_id: uuid.UUID, names: Sequence[str] | None) -> int:
        stmt = text(
            "SELECT count(*) FROM metric_series WHERE org_id = :org AND service_id = :svc"
            " AND (CAST(:names AS text[]) IS NULL OR name = ANY(CAST(:names AS text[])))"
        )
        return int(
            await self.session.scalar(
                stmt,
                {
                    "org": self.ctx.org_id,
                    "svc": service_id,
                    "names": list(names) if names else None,
                },
            )
            or 0
        )

    async def deltas(
        self,
        service_id: uuid.UUID,
        baseline: Window,
        window: Window,
        names: Sequence[str] | None,
        max_series: int,
    ) -> list[dict[str, Any]]:
        rows = await self.session.execute(
            _METRIC_DELTAS_SQL,
            {
                "org": self.ctx.org_id,
                "svc": service_id,
                "names": list(names) if names else None,
                "max_series": max_series,
                "bs": baseline.start,
                "be": baseline.end,
                "ws": window.start,
                "we": window.end,
                "k_hist": METRIC_HISTOGRAM,
                "k_gauge": METRIC_GAUGE,
                "k_sum": METRIC_SUM,
            },
        )
        return [dict(r._mapping) for r in rows]


# --- traces -----------------------------------------------------------------------------------


class TraceEvidenceQueries(TenantRepository[Span]):
    model = Span

    async def operations(
        self,
        service_ids: Sequence[uuid.UUID],
        baseline: Window,
        window: Window,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Per (service, operation): spans, errors, p95 before vs during, for inbound (SERVER)
        and database (DB CLIENT) operations. Each branch repeats a partial index's predicate as
        a literal (`ix_spans_server_window`, `ix_spans_db_client_window`), so neither scans a
        whole partition; there is deliberately no general span index on the ingest path."""
        branch = (
            "SELECT service_id, left(name, 256) AS operation, kind,"
            "  count(*) FILTER (WHERE start_time < :be) AS b_n,"
            "  count(*) FILTER (WHERE start_time < :be AND status_code = 2) AS b_err,"
            "  percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)"
            "     FILTER (WHERE start_time < :be) AS b_p95,"
            "  count(*) FILTER (WHERE start_time >= :ws) AS d_n,"
            "  count(*) FILTER (WHERE start_time >= :ws AND status_code = 2) AS d_err,"
            "  percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)"
            "     FILTER (WHERE start_time >= :ws) AS d_p95"
            " FROM spans WHERE org_id = :org AND service_id = ANY(:svcs)"
            "  AND {predicate} AND start_time >= :bs AND start_time < :we"
            "  AND (start_time < :be OR start_time >= :ws)"
            " GROUP BY 1, 2, 3"
        )
        server = branch.format(predicate="kind = 2")
        db_client = branch.format(predicate="kind = 3 AND db_system IS NOT NULL")
        rows = await self.session.execute(
            text(
                f"SELECT * FROM ({server} UNION ALL {db_client}) ops"  # noqa: S608 (constants)
                " ORDER BY d_err DESC, d_n DESC, service_id, operation, kind LIMIT :lim"
            ),
            {
                "org": self.ctx.org_id,
                "svcs": list(service_ids),
                "bs": baseline.start,
                "be": baseline.end,
                "ws": window.start,
                "we": window.end,
                "lim": limit,
            },
        )
        return [dict(r._mapping) for r in rows]

    async def span_exceptions(
        self, service_ids: Sequence[uuid.UUID], w: Window, limit: int
    ) -> list[dict[str, Any]]:
        rows = await self.session.execute(
            text(
                "SELECT service_id, left(exception_type, 256) AS exception_type,"
                "  left(coalesce(exception_message, status_message), 1000) AS exception_message,"
                "  count(*) AS n"
                " FROM spans WHERE org_id = :org AND service_id = ANY(:svcs) AND status_code = 2"
                "  AND start_time >= :s AND start_time < :e"
                "  AND (exception_type IS NOT NULL OR exception_message IS NOT NULL"
                "       OR status_message IS NOT NULL)"
                " GROUP BY 1, 2, 3 ORDER BY n DESC, 2, 3 LIMIT :lim"
            ),
            {
                "org": self.ctx.org_id,
                "svcs": list(service_ids),
                "s": w.start,
                "e": w.end,
                "lim": limit,
            },
        )
        return [dict(r._mapping) for r in rows]

    async def sample_traces(
        self, service_ids: Sequence[uuid.UUID], w: Window, *, slowest: int, erroring: int
    ) -> list[tuple[bytes, str]]:
        """Trace ids of the slowest SERVER spans and the most recent erroring spans."""
        params = {"org": self.ctx.org_id, "svcs": list(service_ids), "s": w.start, "e": w.end}
        slow = await self.session.execute(
            text(
                "SELECT trace_id FROM spans WHERE org_id = :org AND service_id = ANY(:svcs)"
                " AND kind = 2 AND start_time >= :s AND start_time < :e"
                " ORDER BY duration_ms DESC, trace_id LIMIT :lim"
            ),
            {**params, "lim": slowest * 4},
        )
        err = await self.session.execute(
            text(
                "SELECT trace_id FROM spans WHERE org_id = :org AND service_id = ANY(:svcs)"
                " AND status_code = 2 AND start_time >= :s AND start_time < :e"
                " ORDER BY start_time DESC, trace_id LIMIT :lim"
            ),
            {**params, "lim": erroring * 4},
        )
        out: list[tuple[bytes, str]] = []
        seen: set[bytes] = set()
        for result, label, n in ((slow, "slowest", slowest), (err, "erroring", erroring)):
            picked = 0
            for (trace,) in result:
                t = bytes(trace)
                if t in seen or picked >= n:
                    continue
                seen.add(t)
                out.append((t, label))
                picked += 1
        return out

    async def trace_spans(
        self, trace_id: bytes, start: datetime, end: datetime, limit: int
    ) -> list[Span]:
        stmt = (
            self.scoped()
            .where(Span.trace_id == trace_id, Span.start_time >= start, Span.start_time < end)
            .order_by(Span.start_time, Span.span_id)
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())

    async def edge_server_stats(
        self, source: uuid.UUID, target: uuid.UUID, baseline: Window, window: Window
    ) -> dict[str, Any]:
        """Target's SERVER spans whose parent is a `source` span: calls, errors, p95 before vs
        during (the edge's own traffic)."""
        row = await self.session.execute(
            text(
                "SELECT"
                "  count(*) FILTER (WHERE c.start_time < :be) AS b_n,"
                "  count(*) FILTER (WHERE c.start_time < :be AND c.status_code = 2) AS b_err,"
                "  percentile_cont(0.95) WITHIN GROUP (ORDER BY c.duration_ms)"
                "     FILTER (WHERE c.start_time < :be) AS b_p95,"
                "  count(*) FILTER (WHERE c.start_time >= :ws) AS d_n,"
                "  count(*) FILTER (WHERE c.start_time >= :ws AND c.status_code = 2) AS d_err,"
                "  percentile_cont(0.95) WITHIN GROUP (ORDER BY c.duration_ms)"
                "     FILTER (WHERE c.start_time >= :ws) AS d_p95"
                " FROM spans c JOIN spans p ON p.org_id = :org AND p.trace_id = c.trace_id"
                "  AND p.span_id = c.parent_span_id AND p.service_id = :src"
                "  AND p.start_time >= :pf AND p.start_time < :we"
                " WHERE c.org_id = :org AND c.service_id = :tgt AND c.kind = 2"
                "  AND c.start_time >= :bs AND c.start_time < :we"
                "  AND (c.start_time < :be OR c.start_time >= :ws)"
            ),
            {
                "org": self.ctx.org_id,
                "src": source,
                "tgt": target,
                # A parent starts before its child; bounding it prunes partitions.
                "pf": baseline.start - timedelta(minutes=5),
                "bs": baseline.start,
                "be": baseline.end,
                "ws": window.start,
                "we": window.end,
            },
        )
        return dict(row.one()._mapping)

    async def edge_client_stats(
        self, source: uuid.UUID, db_system: str, target: str, baseline: Window, window: Window
    ) -> dict[str, Any]:
        """Source's DB CLIENT spans to one external target: calls, errors, p95 before vs during."""
        row = await self.session.execute(
            text(
                "SELECT"
                "  count(*) FILTER (WHERE start_time < :be) AS b_n,"
                "  count(*) FILTER (WHERE start_time < :be AND status_code = 2) AS b_err,"
                "  percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)"
                "     FILTER (WHERE start_time < :be) AS b_p95,"
                "  count(*) FILTER (WHERE start_time >= :ws) AS d_n,"
                "  count(*) FILTER (WHERE start_time >= :ws AND status_code = 2) AS d_err,"
                "  percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms)"
                "     FILTER (WHERE start_time >= :ws) AS d_p95"
                " FROM spans WHERE org_id = :org AND service_id = :src AND kind = 3"
                "  AND db_system = :sys"
                "  AND coalesce(attributes ->> 'db.namespace', server_address) = :tgt"
                "  AND start_time >= :bs AND start_time < :we"
                "  AND (start_time < :be OR start_time >= :ws)"
            ),
            {
                "org": self.ctx.org_id,
                "src": source,
                "sys": db_system,
                "tgt": target,
                "bs": baseline.start,
                "be": baseline.end,
                "ws": window.start,
                "we": window.end,
            },
        )
        return dict(row.one()._mapping)


class EdgeEvidenceQueries(TenantRepository[ServiceEdge]):
    model = ServiceEdge

    async def around(self, service_ids: Sequence[uuid.UUID], limit: int) -> list[ServiceEdge]:
        """Edges with either end in `service_ids` (any source), deterministic order."""
        if not service_ids:
            return []
        ids = list(service_ids)
        stmt = (
            self.scoped()
            .where(
                or_(ServiceEdge.source_service_id.in_(ids), ServiceEdge.target_service_id.in_(ids))
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


__all__ = [
    "EdgeEvidenceQueries",
    "LogEvidenceQueries",
    "MetricEvidenceQueries",
    "TraceEvidenceQueries",
    "Window",
    "bound_statement_time",
]
