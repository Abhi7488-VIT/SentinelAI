"""Detection tick (Phase 3 T7, ADR 0006): rollup → rules → state machine → detections.

Runs every 15 s from beat. Evaluation time `eval_end` = now - 30 s, floored to the 15-s grid,
so late-arriving spans do not look like a drop. One transaction per tick under an advisory lock
(overlapping ticks skip); per detector, a tick whose `eval_end` was already evaluated is a no-op,
so a retried tick changes nothing, and detections are unique on (service, rule, started_at).

Deterministic only: SQL aggregation + the pure rules in `app.detection` (invariant 8).
"""

from __future__ import annotations

import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.tenancy import SystemScope
from app.detection.explain import explain
from app.detection.rules import (
    CRASH_LOOP_WINDOW_S,
    CRITICAL_AFTER_S,
    DEPLOY_WINDOW_S,
    LATENCY_BASELINE_MINUTES,
    Cond,
    Evaluation,
    MemoryReading,
    PoolReading,
    Rule,
    RuleParams,
    Severity,
    error_rate,
    health_check,
    latency_shift,
    params_for,
    resource_saturation,
)
from app.detection.signatures import normalize_message, signature
from app.detection.state_machine import Snapshot, State, step
from app.models.detection import Detection, DetectorState
from app.models.service import HealthStatus, Service
from app.repositories.detection import (
    BUCKET,
    DeploymentQueries,
    DetectionRepository,
    DetectorStateRepository,
    ExceptionCount,
    MetricReadingQueries,
    ServiceInstanceRepository,
    SpanEvidenceQueries,
    WindowStatsRepository,
    orgs_with_open_detections,
    try_tick_lock,
)
from app.repositories.service_graph import ServiceEdgeRepository, orgs_with_recent_telemetry
from app.repositories.services import ServiceRepository

logger = get_logger("app.detection")

EVAL_LAG = timedelta(seconds=30)
WINDOW = timedelta(seconds=60)
RECOMPUTE = timedelta(minutes=2)  # recent buckets recomputed every tick (late data)
BACKFILL = timedelta(minutes=35)  # rebuilt after a gap: covers the 30-min latency baseline
ACTIVE_WINDOW = timedelta(minutes=10)
METRIC_WINDOW = timedelta(seconds=60)
HEALTHY_SILENCE = timedelta(seconds=90)
POOL_USED = "db.client.connection.count"
POOL_MAX = "db.client.connection.max"
POOL_PENDING = "db.client.connection.pending_requests"
MEMORY_USAGE = "process.memory.usage"
EVALUATED_RULES = (
    Rule.ERROR_RATE,
    Rule.LATENCY_SHIFT,
    Rule.RESOURCE_SATURATION,
    Rule.HEALTH_CHECK,
)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def floor_bucket(t: datetime) -> datetime:
    seconds = int((t - _EPOCH).total_seconds())
    return _EPOCH + timedelta(seconds=seconds - seconds % int(BUCKET.total_seconds()))


def eval_end_for(now: datetime) -> datetime:
    return floor_bucket(now - EVAL_LAG)


def top_exceptions(counts: Iterable[ExceptionCount], limit: int = 5) -> list[dict[str, Any]]:
    """Group raw (type, message) counts by signature; most frequent first."""
    merged: dict[str, dict[str, Any]] = {}
    for c in counts:
        sig = signature(c.type, c.message)
        entry = merged.setdefault(
            sig,
            {
                "signature": sig,
                "type": (c.type or "")[:256] or None,
                "message": normalize_message(c.message),
                "count": 0,
            },
        )
        entry["count"] += c.count
    return sorted(merged.values(), key=lambda e: (-e["count"], e["signature"]))[:limit]


def merge_bucket_exceptions(buckets: Iterable[Sequence[dict[str, Any]]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for entries in buckets:
        for e in entries:
            m = merged.setdefault(e["signature"], {**e, "count": 0})
            m["count"] += int(e.get("count", 0))
    return sorted(merged.values(), key=lambda e: (-e["count"], e["signature"]))[:5]


def pool_readings(readings: Iterable[Any]) -> list[PoolReading]:
    """Assemble per-(instance, pool) readings from OTel semconv pool metrics."""
    used: dict[tuple[str, str], float] = defaultdict(float)
    maxes: dict[tuple[str, str], float] = {}
    pending: dict[tuple[str, str], float] = defaultdict(float)
    for r in readings:
        key = (
            str(r.attributes.get("service.instance.id", "")),
            str(r.attributes.get("db.client.connection.pool.name", "default")),
        )
        if r.name == POOL_USED and r.attributes.get("db.client.connection.state") == "used":
            used[key] += r.value
        elif r.name == POOL_MAX:
            maxes[key] = r.value
        elif r.name == POOL_PENDING:
            pending[key] += r.value
    return [
        PoolReading(instance=k[0], pool=k[1], used=used[k], max=maxes[k], pending=pending[k])
        for k in sorted(maxes)
        if maxes[k] > 0
    ]


@dataclass
class TickResult:
    ran: bool
    eval_end: datetime | None = None
    orgs: int = 0
    services: int = 0
    evaluated: int = 0
    fired: list[uuid.UUID] = field(default_factory=list)
    resolved: list[uuid.UUID] = field(default_factory=list)
    escalated: list[uuid.UUID] = field(default_factory=list)
    duration_ms: float = 0.0


async def run_tick(session: AsyncSession, *, now: datetime | None = None) -> TickResult:
    started = time.monotonic()
    now = now or datetime.now(UTC)
    if not await try_tick_lock(session):
        await session.rollback()
        logger.info("detection_tick_skipped", reason="locked")
        return TickResult(ran=False)
    eval_end = eval_end_for(now)
    result = TickResult(ran=True, eval_end=eval_end)
    recent = await orgs_with_recent_telemetry(session, now - ACTIVE_WINDOW)
    orgs = sorted(set(recent) | set(await orgs_with_open_detections(session)))
    for org_id in orgs:
        await OrgTick(session, SystemScope(org_id, "detection"), now, eval_end, result).run()
    result.orgs = len(orgs)
    await session.commit()
    result.duration_ms = round((time.monotonic() - started) * 1000, 1)
    logger.info(
        "detection_tick_done",
        eval_end=eval_end.isoformat(),
        orgs=result.orgs,
        services=result.services,
        evaluated=result.evaluated,
        fired=len(result.fired),
        resolved=len(result.resolved),
        escalated=len(result.escalated),
        duration_ms=result.duration_ms,
    )
    return result


class OrgTick:
    def __init__(
        self,
        session: AsyncSession,
        scope: SystemScope,
        now: datetime,
        eval_end: datetime,
        result: TickResult,
    ) -> None:
        self.session = session
        self.now = now
        self.eval_end = eval_end
        self.result = result
        self.services_repo = ServiceRepository(session, scope)
        self.stats = WindowStatsRepository(session, scope)
        self.instances = ServiceInstanceRepository(session, scope)
        self.spans = SpanEvidenceQueries(session, scope)
        self.metrics = MetricReadingQueries(session, scope)
        self.deployments = DeploymentQueries(session, scope)
        self.states = DetectorStateRepository(session, scope)
        self.detections = DetectionRepository(session, scope)
        self.edges = ServiceEdgeRepository(session, scope)

    async def run(self) -> None:
        services = await self.services_repo.active()
        for svc in services:
            if svc.last_seen_at is not None and svc.last_seen_at >= self.eval_end - BACKFILL:
                await self._rollup(svc)
            await self._evaluate(svc)
            self.result.services += 1
        await self.session.flush()
        await self._deploy_changes()
        await self.session.flush()
        await self._health_status(services)

    # ---- rollup -----------------------------------------------------------------------------

    async def _rollup(self, svc: Service) -> None:
        last_bucket = self.eval_end - BUCKET
        recent_first = last_bucket - RECOMPUTE + BUCKET
        backfill_first = last_bucket - BACKFILL + BUCKET
        latest = await self.stats.latest_bucket(svc.id)
        if latest is None:
            first = backfill_first  # first run for this service: build the baseline window
        elif latest >= recent_first:
            first = recent_first
        else:
            first = max(backfill_first, latest)  # after a gap: from the last stored bucket
        rows = await self.stats.compute(svc.id, first, last_bucket)
        by_bucket: dict[datetime, list[ExceptionCount]] = defaultdict(list)
        for c in await self.stats.exception_counts(svc.id, first, self.eval_end):
            by_bucket[c.bucket].append(c)
        await self.stats.upsert(
            svc.id, rows, {b: top_exceptions(cs) for b, cs in by_bucket.items()}
        )
        await self.instances.observe(svc.id, first - WINDOW, self.now)

    # ---- rules ------------------------------------------------------------------------------

    async def _evaluate(self, svc: Service) -> None:
        states = await self.states.for_service(svc.id)
        overrides: dict[str, Any] = (svc.telemetry_config or {}).get("detection") or {}
        for rule in EVALUATED_RULES:
            p = params_for(rule, overrides.get(rule.value))
            st = states.get(rule.value)
            evaluated = st.last_evaluated_at if st is not None else None
            if evaluated is not None and evaluated >= self.eval_end:
                continue  # this bucket was already evaluated (retried / overlapping tick)
            ev = (
                await self._condition(rule, svc, p)
                if p.enabled
                else Evaluation(Cond.CLEAR, "disabled")
            )
            snap = (
                Snapshot(State(st.state), st.since, st.last_evaluated_at)
                if st is not None
                else Snapshot(State.OK, None, None)
            )
            clear_s = p.clear_duration_s if p.enabled else 0
            result = step(
                snap,
                ev.cond,
                self.eval_end,
                min_duration_s=p.min_duration_s,
                clear_duration_s=clear_s,
            )
            open_id = st.open_detection_id if st is not None else None
            if result.fire and result.started_at is not None:
                det = await self._fire(svc, rule, p, ev, result.started_at)
                open_id = det.id
            elif open_id is not None:
                det_open = await self.detections.get(open_id)
                if det_open is not None:
                    if result.resolve:
                        det_open.resolved_at = self.eval_end
                        self.result.resolved.append(det_open.id)
                        open_id = None
                    else:
                        self._update_open(det_open, ev)
            if st is None:
                st = await self.states.add(
                    DetectorState(
                        service_id=svc.id,
                        rule=rule.value,
                        state=result.state.value,
                        since=result.since,
                    )
                )
            st.state = result.state.value
            st.since = result.since
            st.last_cond = ev.cond.value
            st.last_value = ev.value
            st.last_evaluated_at = self.eval_end
            st.open_detection_id = open_id
            self.result.evaluated += 1

    async def _condition(self, rule: Rule, svc: Service, p: RuleParams) -> Evaluation:
        latest_bucket = self.eval_end - BUCKET
        if rule in (Rule.ERROR_RATE, Rule.LATENCY_SHIFT):
            row = await self.stats.at(svc.id, latest_bucket)
            requests = row.requests_60s if row else 0
            if rule is Rule.ERROR_RATE:
                return error_rate(requests, row.errors_60s if row else 0, p)
            return latency_shift(
                row.p95_60s_ms if row else None, requests, await self._latency_baseline(svc, p), p
            )
        if rule is Rule.RESOURCE_SATURATION:
            readings = await self.metrics.latest(
                svc.id,
                [POOL_USED, POOL_MAX, POOL_PENDING, MEMORY_USAGE],
                self.now - METRIC_WINDOW,
                self.now + timedelta(seconds=1),
            )
            limit = (svc.telemetry_config or {}).get("memory_limit_bytes")
            memory = (
                [
                    MemoryReading(str(r.attributes.get("service.instance.id", "")), r.value, limit)
                    for r in readings
                    if r.name == MEMORY_USAGE
                ]
                if isinstance(limit, int) and limit > 0
                else []
            )
            return resource_saturation(pool_readings(readings), memory, p)
        # HEALTH_CHECK
        silent = (
            (self.now - svc.last_seen_at).total_seconds() if svc.last_seen_at is not None else None
        )
        new_5m = await self.instances.first_seen_since(
            svc.id, self.now - timedelta(seconds=CRASH_LOOP_WINDOW_S)
        )
        new_2m = await self.instances.first_seen_since(svc.id, self.now - timedelta(minutes=2))
        return health_check(silent, len(new_5m), len(new_2m), new_5m)

    async def _latency_baseline(self, svc: Service, p: RuleParams) -> list[float]:
        """Per-minute p95 (trailing-60-s p95 at minute boundaries) over the 30 min before the
        current window, minutes with too few samples or while this rule fired excluded."""
        end = self.eval_end - WINDOW
        start = end - timedelta(minutes=LATENCY_BASELINE_MINUTES)
        fired = await self.detections.intervals(svc.id, Rule.LATENCY_SHIFT.value, start, end)
        values: list[float] = []
        for b in await self.stats.between(svc.id, start, end):
            bucket_end = b.bucket_start + BUCKET
            if bucket_end.second != 0 or b.p95_60s_ms is None or b.requests_60s < p.min_samples:
                continue
            minute_start = bucket_end - WINDOW
            if any(s < bucket_end and (r is None or r > minute_start) for s, r in fired):
                continue
            values.append(b.p95_60s_ms)
        return values

    async def _fire(
        self, svc: Service, rule: Rule, p: RuleParams, ev: Evaluation, started_at: datetime
    ) -> Detection:
        window_start = self.eval_end - WINDOW
        traces: list[str] = []
        exceptions: list[dict[str, Any]] = []
        facts = dict(ev.facts)
        if rule is not Rule.HEALTH_CHECK:
            window = await self.stats.between(svc.id, window_start, self.eval_end)
            exceptions = merge_bucket_exceptions(b.exceptions for b in window)
            if rule is Rule.LATENCY_SHIFT:
                traces = await self.spans.slowest_traces(svc.id, window_start, self.eval_end)
            else:
                traces = await self.spans.error_traces(svc.id, window_start, self.eval_end)
                if not traces and rule is Rule.RESOURCE_SATURATION:
                    traces = await self.spans.slowest_traces(svc.id, window_start, self.eval_end)
        if rule is Rule.ERROR_RATE:
            # The 30 min before the first anomalous window (not before the firing window,
            # which would count the anomaly's own start as "baseline").
            base_end = started_at - WINDOW
            base = await self.stats.between(svc.id, base_end - timedelta(minutes=30), base_end)
            requests = sum(b.requests for b in base)
            facts["baseline_ratio"] = sum(b.errors for b in base) / requests if requests else None
        ev_full = Evaluation(
            ev.cond, ev.metric, ev.value, ev.baseline, ev.threshold, ev.critical, facts
        )
        text = explain(
            rule,
            svc.name,
            ev_full,
            window_end=self.eval_end,
            started_at=started_at,
            min_duration_s=p.min_duration_s,
            exceptions=exceptions,
            sample_traces=traces,
        )
        det = await self.detections.insert_once(
            {
                "service_id": svc.id,
                "rule": rule.value,
                "severity": Severity.WARNING.value,
                "metric": ev.metric,
                "observed_value": ev.value,
                "baseline_value": ev.baseline,
                "threshold": ev.threshold,
                "peak_value": ev.value,
                "last_value": ev.value,
                "started_at": started_at,
                "fired_at": self.eval_end,
                "explanation": text,
                "evidence": {
                    "window": {"start": window_start.isoformat(), "end": self.eval_end.isoformat()},
                    "facts": facts,
                    "sample_trace_ids": traces[:20],
                    "exception_signatures": exceptions,
                    "params": {
                        "fire": p.fire,
                        "clear": p.clear,
                        "critical": p.critical,
                        "min_samples": p.min_samples,
                        "min_duration_s": p.min_duration_s,
                        "clear_duration_s": p.clear_duration_s,
                    },
                },
            }
        )
        self.result.fired.append(det.id)
        return det

    def _update_open(self, det: Detection, ev: Evaluation) -> None:
        if ev.value is not None:
            det.last_value = ev.value
            if det.peak_value is None or ev.value > det.peak_value:
                det.peak_value = ev.value
        age = (self.eval_end - det.fired_at).total_seconds()
        if ev.critical and age >= CRITICAL_AFTER_S and det.severity != Severity.CRITICAL.value:
            det.severity = Severity.CRITICAL.value
            self.result.escalated.append(det.id)

    # ---- deploy_change ----------------------------------------------------------------------

    async def _deploy_changes(self) -> None:
        """Link open detections to deployments of their service or a neighbor that happened
        within 10 min before they started; resolve deploy_change once all links resolved."""
        open_all = await self.detections.open_in_org()
        neighbors: dict[uuid.UUID, set[uuid.UUID]] = {}
        names = {s.id: s.name for s in await self.services_repo.active()}
        deploy_dets = {
            (d.service_id, d.started_at): d for d in open_all if d.rule == Rule.DEPLOY_CHANGE.value
        }
        for det in (d for d in open_all if d.rule != Rule.DEPLOY_CHANGE.value):
            if det.service_id not in neighbors:
                neighbors[det.service_id] = await self.edges.neighbors(det.service_id)
            candidates = {det.service_id} | neighbors[det.service_id]
            window = timedelta(seconds=DEPLOY_WINDOW_S)
            for dep in await self.deployments.between(
                sorted(candidates), det.started_at - window, det.started_at
            ):
                key = (dep.service_id, dep.ts)
                change = deploy_dets.get(key)
                if change is None:
                    change = await self._fire_deploy_change(dep.service_id, names, dep)
                    deploy_dets[key] = change
                self._link(change, det, names)
        # resolve deploy changes whose linked detections are all resolved
        for change in (d for d in open_all if d.rule == Rule.DEPLOY_CHANGE.value):
            linked_ids = [uuid.UUID(x) for x in change.evidence.get("linked_detection_ids", [])]
            linked = await self.detections.get_many(linked_ids)
            if linked and all(d.resolved_at is not None for d in linked):
                change.resolved_at = max(self.eval_end, change.fired_at)
                self.result.resolved.append(change.id)

    async def _fire_deploy_change(
        self, service_id: uuid.UUID, names: dict[uuid.UUID, str], dep: Any
    ) -> Detection:
        facts = {
            "deployment_id": dep.deployment_id,
            "version": dep.version,
            "previous_version": dep.previous_version,
            "linked": [],
        }
        ev = Evaluation(Cond.FIRE, "deployment", facts=facts)
        det = await self.detections.insert_once(
            {
                "service_id": service_id,
                "rule": Rule.DEPLOY_CHANGE.value,
                "severity": Severity.WARNING.value,
                "metric": "deployment",
                "started_at": dep.ts,
                "fired_at": max(self.eval_end, dep.ts),
                "explanation": explain(
                    Rule.DEPLOY_CHANGE,
                    names.get(service_id, "?"),
                    ev,
                    window_end=self.eval_end,
                    started_at=dep.ts,
                    min_duration_s=0,
                ),
                "evidence": {
                    "deployment_id": dep.deployment_id,
                    "deployment_row_id": str(dep.id),
                    "version": dep.version,
                    "previous_version": dep.previous_version,
                    "linked_detection_ids": [],
                    "linked": [],
                },
            }
        )
        if det.resolved_at is None and det.id not in self.result.fired:
            self.result.fired.append(det.id)
        return det

    def _link(self, change: Detection, det: Detection, names: dict[uuid.UUID, str]) -> None:
        evidence = dict(change.evidence)
        ids = list(evidence.get("linked_detection_ids", []))
        if str(det.id) in ids:
            return
        ids.append(str(det.id))
        linked = list(evidence.get("linked", []))
        linked.append(
            f"{det.rule} on {names.get(det.service_id, '?')} started "
            f"{det.started_at.strftime('%H:%M:%SZ')}"
        )
        evidence["linked_detection_ids"] = ids
        evidence["linked"] = linked
        change.evidence = evidence  # new object: SQLAlchemy sees the JSONB change
        facts = {**evidence, "linked": linked}
        change.explanation = explain(
            Rule.DEPLOY_CHANGE,
            names.get(change.service_id, "?"),
            Evaluation(Cond.FIRE, "deployment", facts=facts),
            window_end=self.eval_end,
            started_at=change.started_at,
            min_duration_s=0,
        )

    # ---- health status ----------------------------------------------------------------------

    async def _health_status(self, services: Sequence[Service]) -> None:
        severities: dict[uuid.UUID, Counter[str]] = defaultdict(Counter)
        for det in await self.detections.open_in_org():
            severities[det.service_id][det.severity] += 1
        for svc in services:
            counts = severities.get(svc.id)
            if counts and counts[Severity.CRITICAL.value]:
                status = HealthStatus.UNHEALTHY
            elif counts:
                status = HealthStatus.DEGRADED
            elif svc.last_seen_at is not None and self.now - svc.last_seen_at < HEALTHY_SILENCE:
                status = HealthStatus.HEALTHY
            else:
                status = HealthStatus.UNKNOWN
            if svc.health_status != status.value:
                svc.health_status = status.value
