"""Correlation of detections into incidents (Phase 3 T8, plan §7, ADR 0006).

Runs inside the detection tick, after detections are written (same transaction, same lock).
Each detection that is in no incident yet is scored against the project's unresolved incidents
whose last activity is within 10 min of the detection's start:

- `shared_trace` (3): a sample trace of one side has an ERROR span in the other side's services;
- `exception_signature` (3): the same normalized exception signature;
- `dependency_edge` (2): a direct edge (either direction, declared or observed) to an incident
  service;
- `deploy_proximity` (2): both are linked to the same deployment by a `deploy_change` detection;
- `same_service` (1): the incident already has a detection on the same service.

A detection joins only with at least one reason: time proximity alone never merges. Highest
score wins, ties go to the oldest incident; no candidate → a new incident. Incident fields are
derived (title from services and rules, severity = max, started = earliest), never a root cause.
Every change writes an `incident_events` row. The system never resolves an incident; once all of
its detections have been resolved for 10 min it records `all_detections_resolved`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import SystemScope
from app.detection.rules import SEVERITY_RANK, Rule, Severity
from app.models.detection import Detection
from app.models.incident import Incident, IncidentEvent
from app.models.service import Service
from app.repositories.detection import DetectionRepository
from app.repositories.incidents import (
    CorrelationQueries,
    IncidentDetectionRepository,
    IncidentEventRepository,
    IncidentRepository,
    IncidentServiceRepository,
)
from app.repositories.services import ServiceRepository

CORRELATION_WINDOW = timedelta(minutes=10)
RESOLVED_QUIET = timedelta(minutes=10)
# Detections are picked up for correlation for this long after firing (a retried or delayed
# tick still links them; older unlinked detections are left alone).
PICKUP_WINDOW = timedelta(minutes=30)
SCORES = {
    "shared_trace": 3,
    "exception_signature": 3,
    "dependency_edge": 2,
    "deploy_proximity": 2,
    "same_service": 1,
}
_RULE_NOUNS = {
    Rule.ERROR_RATE.value: "errors",
    Rule.LATENCY_SHIFT.value: "latency",
    Rule.RESOURCE_SATURATION.value: "resource saturation",
    Rule.HEALTH_CHECK.value: "health check failures",
    Rule.DEPLOY_CHANGE.value: "a deployment change",
}


def incident_title(rules: Sequence[str], services: Sequence[str]) -> str:
    """'Errors and latency in order-service, api-gateway, frontend' (first-seen order)."""
    nouns = list(dict.fromkeys(_RULE_NOUNS.get(r, r) for r in rules))
    names = list(dict.fromkeys(services))
    what = nouns[0] if len(nouns) == 1 else ", ".join(nouns[:-1]) + " and " + nouns[-1]
    where = ", ".join(names[:3]) + (f" +{len(names) - 3} more" if len(names) > 3 else "")
    return f"{what[0].upper()}{what[1:]} in {where}"[:300]


def max_severity(values: Iterable[str]) -> str:
    return max(values, key=lambda s: SEVERITY_RANK[Severity(s)], default=Severity.WARNING.value)


def _signatures(det: Detection) -> set[str]:
    return {
        str(e["signature"])
        for e in det.evidence.get("exception_signatures", [])
        if isinstance(e, dict) and e.get("signature")
    }


def _traces(det: Detection) -> list[bytes]:
    out = []
    for t in det.evidence.get("sample_trace_ids", [])[:20]:
        try:
            out.append(bytes.fromhex(str(t)))
        except ValueError:
            continue
    return out


@dataclass
class CorrelationResult:
    created: list[uuid.UUID] = field(default_factory=list)
    joined: list[uuid.UUID] = field(default_factory=list)


class Correlator:
    def __init__(self, session: AsyncSession, scope: SystemScope, eval_end: datetime) -> None:
        self.eval_end = eval_end
        self.incidents = IncidentRepository(session, scope)
        self.links = IncidentDetectionRepository(session, scope)
        self.inc_services = IncidentServiceRepository(session, scope)
        self.events = IncidentEventRepository(session, scope)
        self.queries = CorrelationQueries(session, scope)
        self.services = ServiceRepository(session, scope)
        self.detections = DetectionRepository(session, scope)
        self.session = session
        self.result = CorrelationResult()

    async def run(self) -> CorrelationResult:
        pending = await self.links.unlinked_detections(self.eval_end - PICKUP_WINDOW)
        if pending:
            svc_ids = sorted({d.service_id for d in pending})
            self._svc = {s.id: s for s in await self.services.get_many(svc_ids)}
            # deployment id → detection ids linked by its deploy_change detection(s).
            self._deploys: dict[str, set[str]] = {}
            since = self.eval_end - PICKUP_WINDOW - CORRELATION_WINDOW
            for change in await self.detections.fired_since(Rule.DEPLOY_CHANGE.value, since):
                dep = change.evidence.get("deployment_id")
                if dep:
                    self._deploys.setdefault(str(dep), set()).update(
                        str(x) for x in change.evidence.get("linked_detection_ids", [])
                    )
            for det in pending:
                await self._correlate(det)
                await self.session.flush()
        await self._refresh()
        return self.result

    # ---- one detection ----------------------------------------------------------------------

    async def _correlate(self, det: Detection) -> None:
        svc = self._svc.get(det.service_id)
        if svc is None:
            return
        candidates = await self.incidents.active_in_project(
            svc.project_id, det.started_at - CORRELATION_WINDOW
        )
        members = await self.links.members([c.id for c in candidates])
        best: tuple[Incident, list[dict[str, Any]], int] | None = None
        for inc in candidates:  # oldest first: a tie keeps the older incident
            dets = [d for d, _ in members.get(inc.id, [])]
            reasons = await self._reasons(det, inc, dets)
            score = sum(SCORES[r["type"]] for r in reasons)
            if reasons and (best is None or score > best[2]):
                best = (inc, reasons, score)
        if best is None:
            await self._open_incident(det, svc)
        else:
            await self._join(best[0], det, best[1], best[2], members.get(best[0].id, []))

    async def _reasons(
        self, det: Detection, inc: Incident, members: list[Detection]
    ) -> list[dict[str, Any]]:
        reasons: list[dict[str, Any]] = []
        member_services = sorted({m.service_id for m in members})
        others = [s for s in member_services if s != det.service_id]
        since = min(det.started_at, inc.started_at) - CORRELATION_WINDOW

        # shared_trace (both directions)
        hits = await self.queries.traces_with_errors(_traces(det), member_services, since)
        if not hits:
            member_traces = [t for m in members for t in _traces(m)]
            hits = await self.queries.traces_with_errors(member_traces, [det.service_id], since)
        if hits:
            reasons.append(
                {
                    "type": "shared_trace",
                    "detail": f"{len(hits)} sample trace(s) have error spans on both sides",
                    "refs": [h.hex() for h in hits],
                }
            )
        # exception_signature
        common = sorted(_signatures(det) & set().union(*(_signatures(m) for m in members)))
        if common:
            reasons.append(
                {
                    "type": "exception_signature",
                    "detail": "same exception signature",
                    "refs": common[:5],
                }
            )
        # dependency_edge
        edges = await self.queries.edges_touching(det.service_id, others)
        if edges:
            reasons.append(
                {
                    "type": "dependency_edge",
                    "detail": f"direct dependency edge to {len(_neighbours(det, edges))} "
                    "incident service(s)",
                    "refs": [{"edge_id": str(e.id), "source": e.source} for e in edges[:10]],
                }
            )
        # deploy_proximity (shared deploy_change link)
        shared = self._deploy_group(det) & set().union(*(self._deploy_group(m) for m in members))
        if shared:
            reasons.append(
                {
                    "type": "deploy_proximity",
                    "detail": "linked to the same deployment",
                    "refs": sorted(shared)[:5],
                }
            )
        # same_service
        same = [str(m.id) for m in members if m.service_id == det.service_id]
        if same:
            reasons.append(
                {
                    "type": "same_service",
                    "detail": "the incident already has a detection on this service",
                    "refs": same[:5],
                }
            )
        return reasons

    def _deploy_group(self, det: Detection) -> set[str]:
        """Deployment ids this detection is tied to: its own (deploy_change) or the deploy_change
        detections that link it (read from their evidence, already written this tick)."""
        if det.rule == Rule.DEPLOY_CHANGE.value:
            dep = det.evidence.get("deployment_id")
            return {str(dep)} if dep else set()
        return {dep for dep, ids in self._deploys.items() if str(det.id) in ids}

    # ---- writes -----------------------------------------------------------------------------

    async def _open_incident(self, det: Detection, svc: Service) -> None:
        inc = await self.incidents.add(
            Incident(
                project_id=svc.project_id,
                number=await self.incidents.next_number(),
                title=incident_title([det.rule], [svc.name]),
                status="open",
                severity=det.severity,
                started_at=det.started_at,
                detected_at=det.fired_at,
                last_activity_at=max(self.eval_end, det.fired_at),
            )
        )
        await self.links.link(inc.id, det.id, [], 0)
        await self.inc_services.add_service(inc.id, det.service_id)
        await self._event(
            inc,
            "created",
            {
                "detection_id": str(det.id),
                "rule": det.rule,
                "service": svc.name,
                "title": inc.title,
            },
        )
        await self._event(inc, "detection_added", self._added_payload(det, svc, [], 0))
        self.result.created.append(inc.id)

    async def _join(
        self,
        inc: Incident,
        det: Detection,
        reasons: list[dict[str, Any]],
        score: int,
        members: list[tuple[Detection, Any]],
    ) -> None:
        if not await self.links.link(inc.id, det.id, reasons, score):
            return
        await self.inc_services.add_service(inc.id, det.service_id)
        dets = [d for d, _ in members] + [det]
        names = await self._names([d.service_id for d in dets])
        dets.sort(key=lambda d: (d.started_at, names[d.service_id], d.rule))
        inc.title = incident_title([d.rule for d in dets], [names[d.service_id] for d in dets])
        inc.started_at = min(inc.started_at, det.started_at)
        inc.last_activity_at = max(inc.last_activity_at, self.eval_end, det.fired_at)
        inc.detections_resolved_at = None
        svc = self._svc[det.service_id]
        await self._event(inc, "detection_added", self._added_payload(det, svc, reasons, score))
        await self._set_severity(inc, max_severity(d.severity for d in dets))
        self.result.joined.append(inc.id)

    async def _refresh(self) -> None:
        """Severity follows its detections; activity while any is open; the quiet marker."""
        active = await self.incidents.unresolved()
        members = await self.links.members([i.id for i in active])
        for inc in active:
            dets = [d for d, _ in members.get(inc.id, [])]
            if not dets:
                continue
            await self._set_severity(inc, max_severity(d.severity for d in dets))
            if any(d.resolved_at is None for d in dets):
                inc.last_activity_at = max(inc.last_activity_at, self.eval_end)
                continue
            last = max(d.resolved_at for d in dets if d.resolved_at is not None)
            if inc.detections_resolved_at is None and self.eval_end - last >= RESOLVED_QUIET:
                inc.detections_resolved_at = self.eval_end
                await self._event(
                    inc,
                    "all_detections_resolved",
                    {"last_resolved_at": last.isoformat(), "detections": len(dets)},
                )

    async def _set_severity(self, inc: Incident, severity: str) -> None:
        if severity != inc.severity:
            await self._event(inc, "severity_changed", {"from": inc.severity, "to": severity})
            inc.severity = severity

    async def _event(self, inc: Incident, type_: str, payload: dict[str, Any]) -> None:
        await self.events.add(
            IncidentEvent(incident_id=inc.id, type=type_, actor="system", payload=payload)
        )

    async def _names(self, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
        missing = [i for i in set(ids) if i not in self._svc]
        for s in await self.services.get_many(missing):
            self._svc[s.id] = s
        return {i: s.name for i, s in self._svc.items()}

    def _added_payload(
        self, det: Detection, svc: Service, reasons: list[dict[str, Any]], score: int
    ) -> dict[str, Any]:
        return {
            "detection_id": str(det.id),
            "rule": det.rule,
            "service": svc.name,
            "severity": det.severity,
            "reasons": reasons,
            "score": score,
        }


def _neighbours(det: Detection, edges: Iterable[Any]) -> set[uuid.UUID]:
    return {
        e.target_service_id if e.source_service_id == det.service_id else e.source_service_id
        for e in edges
    }
