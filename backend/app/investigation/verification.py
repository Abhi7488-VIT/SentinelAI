"""Deterministic verification of hypotheses (Phase 4 plan §6, invariants 1, 2, 8).

Pure functions over the investigation's stored evidence rows: no LLM, no I/O. Each taxonomy
category has named checks; each check returns `pass`, `fail`, `not_run` (the evidence it needs was
not collected) or `not_applicable` (an optional check whose trigger is absent). A check result
lists the evidence ids it looked at, all of which come from the `evidence` table, never from the
model.

Status (plan §6):
- `confirmed`: supporting citations valid, every applicable check of the category passes, and no
  valid contradicting evidence is cited (in Phase 4 "confirmed by direct evidence"; no
  reproduction until Phase 5).
- `probable`: citations valid and at least one check passes, but not all, or a contradiction holds.
- `inconclusive`: everything else (no valid supporting citation, no passing check, category
  `other`).
Model confidence is carried for display and is never read here.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from app.investigation.taxonomy import TAXONOMY

Result = Literal["pass", "fail", "not_run", "not_applicable"]
Status = Literal["confirmed", "probable", "inconclusive"]
STATUS_ORDER: dict[str, int] = {"confirmed": 0, "probable": 1, "inconclusive": 2}

# Thresholds (generic, not tuned to any scenario; ADR 0003).
ERROR_RISE_PP = 5.0  # error-rate rise in percentage points
LATENCY_RISE_PCT = 50.0  # p95 rise
TRAFFIC_DROP_PCT = -50.0
DEPLOY_WINDOW_MIN = 30.0
SATURATION = re.compile(
    r"pool|timeout|timed out|exhaust|saturat|too many connections|queue full|"
    r"rejected|no available|waiting for a connection",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class EvidenceView:
    """What verification needs from an `evidence` row."""

    id: str
    kind: str
    data: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    result: Result
    detail: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Verdict:
    status: Status
    checks: tuple[CheckResult, ...]
    supporting: tuple[str, ...]
    contradicting: tuple[str, ...]
    rejected: tuple[str, ...]
    reason: str

    @property
    def passed(self) -> int:
        return sum(c.result == "pass" for c in self.checks)

    @property
    def applicable(self) -> int:
        return sum(c.result != "not_applicable" for c in self.checks)


@dataclass
class Facts:
    """Indexes over the evidence, by service name."""

    detections: list[dict[str, Any]] = field(default_factory=list)
    detection_ids: list[str] = field(default_factory=list)
    logs: dict[str, list[tuple[str, dict[str, Any]]]] = field(default_factory=dict)
    ops: dict[str, list[tuple[str, dict[str, Any]]]] = field(default_factory=dict)
    signatures: dict[str, list[tuple[str, dict[str, Any]]]] = field(default_factory=dict)
    paths: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    deployments: dict[str, list[tuple[str, dict[str, Any]]]] = field(default_factory=dict)
    deploy_ids: list[str] = field(default_factory=list)
    edges: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    trace_ids: list[str] = field(default_factory=list)

    @classmethod
    def build(cls, evidence: Sequence[EvidenceView]) -> Facts:
        f = cls()
        for ev in sorted(evidence, key=lambda e: e.id):
            d = ev.data
            if ev.kind == "detection_ref":
                f.detection_ids.append(ev.id)
                f.detections += [{**x, "_ev": ev.id} for x in d.get("detections", [])]
            elif ev.kind == "log_query":
                f.logs.setdefault(str(d.get("service")), []).extend(
                    (ev.id, c) for c in d.get("clusters", [])
                )
            elif ev.kind == "trace_analysis":
                f.trace_ids.append(ev.id)
                for op in d.get("operations", []):
                    f.ops.setdefault(str(op.get("service")), []).append((ev.id, op))
                for sig in d.get("exception_signatures", []):
                    f.signatures.setdefault(str(sig.get("service")), []).append((ev.id, sig))
                f.paths += [(ev.id, p) for p in d.get("critical_paths", [])]
            elif ev.kind == "deploy_lookup":
                f.deploy_ids.append(ev.id)
                for dep in d.get("deployments", []):
                    f.deployments.setdefault(str(dep.get("service")), []).append((ev.id, dep))
            elif ev.kind == "dependency_lookup":
                f.edges += [(ev.id, e) for e in d.get("edges", [])]
        return f

    def first_symptom(self, service: str) -> tuple[datetime | None, str]:
        starts = [
            (_t(d["started_at"]), d["_ev"])
            for d in self.detections
            if d.get("service") == service and d.get("started_at")
        ]
        if not starts:
            return None, ""
        return min(starts)

    def symptoms_of_others(self, services: set[str]) -> list[tuple[datetime, str, str]]:
        return sorted(
            (_t(d["started_at"]), str(d.get("service")), d["_ev"])
            for d in self.detections
            if d.get("service") not in services and d.get("started_at")
        )


def _t(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _ids(*groups: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(i for g in groups for i in g))


def _result(name: str, result: Result, detail: str, *ids: Sequence[str]) -> CheckResult:
    return CheckResult(name, result, detail[:500], _ids(*ids))


# --- checks ------------------------------------------------------------------------------------

Check = Callable[[Facts, set[str]], CheckResult]


def saturation_signature(f: Facts, subj: set[str]) -> CheckResult:
    name = "saturation_or_timeout_signature_in_subject"
    seen: list[str] = []
    for svc in sorted(subj):
        for ev, c in f.logs.get(svc, []):
            seen.append(ev)
            if SATURATION.search(str(c.get("template", ""))):
                return _result(name, "pass", f"log cluster on {svc}: {c.get('template')}", [ev])
        for ev, s in f.signatures.get(svc, []):
            seen.append(ev)
            text = f"{s.get('type') or ''} {s.get('message') or ''}"
            if SATURATION.search(text):
                return _result(name, "pass", f"exception on {svc}: {text.strip()}", [ev])
    if not seen:
        return _result(name, "not_run", "no log or trace evidence for the subject")
    return _result(name, "fail", "no saturation/timeout signature in the subject's evidence", seen)


def subject_first(f: Facts, subj: set[str]) -> CheckResult:
    name = "subject_onset_not_after_first_downstream_symptom"
    starts = [(t, ev) for s in subj for t, ev in [f.first_symptom(s)] if t is not None]
    if not starts:
        return _result(name, "not_run", "no detection on the subject to time its onset")
    t_subj, ev_subj = min(starts)
    others = f.symptoms_of_others(subj)
    if not others:
        return _result(name, "pass", "the subject's detection is the only symptom", [ev_subj])
    t_other, svc, ev_other = others[0]
    if t_subj <= t_other:
        return _result(
            name,
            "pass",
            f"subject onset {t_subj.isoformat()} <= {svc} {t_other.isoformat()}",
            [ev_subj, ev_other],
        )
    return _result(
        name, "fail", f"{svc} showed symptoms first ({t_other.isoformat()})", [ev_subj, ev_other]
    )


def outbound_errors_rose(f: Facts, subj: set[str]) -> CheckResult:
    name = "subject_outbound_or_db_error_rate_rose"
    client = [
        (ev, op) for s in sorted(subj) for ev, op in f.ops.get(s, []) if op.get("kind") == "client"
    ]
    if not client:
        return _result(name, "not_run", "no outbound/database operations of the subject observed")
    rose = [(ev, op) for ev, op in client if (op.get("error_pct_change_pp") or 0) >= ERROR_RISE_PP]
    if rose:
        ev, op = rose[0]
        return _result(
            name, "pass", f"{op['operation']} error rate +{op['error_pct_change_pp']} pp", [ev]
        )
    return _result(name, "fail", "no outbound error-rate rise", [ev for ev, _ in client])


def latency_rose(f: Facts, subj: set[str]) -> CheckResult:
    name = "operation_latency_rose"
    ops = [(ev, op) for s in sorted(subj) for ev, op in f.ops.get(s, [])]
    if not ops:
        return _result(name, "not_run", "no operations of the subject observed")
    rose = [(ev, op) for ev, op in ops if (op.get("p95_change_pct") or 0) >= LATENCY_RISE_PCT]
    if rose:
        ev, op = max(rose, key=lambda x: (x[1].get("p95_change_pct") or 0, x[0]))
        return _result(name, "pass", f"{op['operation']} p95 +{op['p95_change_pct']}%", [ev])
    return _result(name, "fail", "no operation's p95 rose", [ev for ev, _ in ops])


def self_time_dominates(f: Facts, subj: set[str]) -> CheckResult:
    name = "operation_self_time_dominates_critical_path_increase"
    slow = [(ev, p) for ev, p in f.paths if p.get("reason") == "slowest" and p.get("path")]
    if not slow:
        return _result(name, "not_run", "no slow-trace critical paths collected")
    ev, p = slow[0]
    worst = max(p["path"], key=lambda e: e.get("self_ms") or 0)
    if worst.get("service") in subj:
        return _result(
            name,
            "pass",
            f"{worst['service']}:{worst.get('operation')} {worst.get('self_ms')} ms",
            [ev],
        )
    return _result(name, "fail", f"most self-time in {worst.get('service')}", [ev])


def no_earlier_errors_elsewhere(f: Facts, subj: set[str]) -> CheckResult:
    name = "no_earlier_error_rate_change_elsewhere"
    starts = [(t, ev) for s in subj for t, ev in [f.first_symptom(s)] if t is not None]
    if not starts:
        return _result(name, "not_run", "no detection on the subject to time its onset")
    t_subj, ev_subj = min(starts)
    earlier = [
        d
        for d in f.detections
        if d.get("service") not in subj
        and d.get("rule") == "error_rate"
        and d.get("started_at")
        and _t(d["started_at"]) < t_subj
    ]
    if earlier:
        return _result(
            name,
            "fail",
            f"error rate rose first on {earlier[0]['service']}",
            [ev_subj, earlier[0]["_ev"]],
        )
    return _result(name, "pass", "no earlier error-rate change on another service", [ev_subj])


def edge_errors_to_target(f: Facts, subj: set[str]) -> CheckResult:
    name = "connection_errors_on_edge_to_target"
    into = [(ev, e) for ev, e in f.edges if e.get("target") in subj and e.get("stats")]
    if not into:
        return _result(name, "not_run", "no edge traffic into the subject collected")
    rose = [
        (ev, e) for ev, e in into if (e["stats"].get("error_pct_change_pp") or 0) >= ERROR_RISE_PP
    ]
    if rose:
        ev, e = rose[0]
        return _result(
            name,
            "pass",
            f"{e['source']} -> {e['target']} errors +{e['stats']['error_pct_change_pp']} pp",
            [ev],
        )
    return _result(name, "fail", "no error rise on edges into the subject", [ev for ev, _ in into])


def target_inbound_degraded(f: Facts, subj: set[str]) -> CheckResult:
    name = "target_inbound_traffic_dropped_or_errored"
    server = [
        (ev, op) for s in sorted(subj) for ev, op in f.ops.get(s, []) if op.get("kind") == "server"
    ]
    if not server:
        return _result(name, "not_run", "no inbound operations of the subject observed")
    for ev, op in server:
        b, d = op.get("baseline", {}), op.get("incident", {})
        drop = (b.get("spans") or 0) and ((d.get("spans") or 0) - b["spans"]) / b["spans"] * 100
        if (op.get("error_pct_change_pp") or 0) >= ERROR_RISE_PP or (
            drop and drop <= TRAFFIC_DROP_PCT
        ):
            return _result(name, "pass", f"{op['operation']} inbound degraded", [ev])
    return _result(
        name,
        "fail",
        "the subject's inbound traffic did not drop or error",
        [ev for ev, _ in server],
    )


def onset_order(f: Facts, subj: set[str]) -> CheckResult:
    name = "onset_order_consistent"
    callers = {e.get("source") for _, e in f.edges if e.get("target") in subj} - subj
    starts = [(t, ev) for s in subj for t, ev in [f.first_symptom(s)] if t is not None]
    caller_starts = [
        (t, ev, c) for c in callers for t, ev in [f.first_symptom(str(c))] if t is not None
    ]
    if not starts or not caller_starts:
        return _result(name, "not_run", "onsets of the subject and its callers not both known")
    t_subj, ev_subj = min(starts)
    t_c, ev_c, c = min(caller_starts)
    res: Result = "pass" if t_subj <= t_c else "fail"
    return _result(
        name, res, f"subject {t_subj.isoformat()} vs caller {c} {t_c.isoformat()}", [ev_subj, ev_c]
    )


def recent_deploy(f: Facts, subj: set[str]) -> CheckResult:
    name = "subject_deployed_within_30_min_before_onset"
    if not f.deploy_ids:
        return _result(name, "not_run", "deployments were not looked up")
    deps = [(ev, d) for s in sorted(subj) for ev, d in f.deployments.get(s, [])]
    hits = [
        (ev, d) for ev, d in deps if 0 <= (d.get("minutes_before_onset") or -1) <= DEPLOY_WINDOW_MIN
    ]
    if hits:
        ev, d = hits[0]
        return _result(
            name,
            "pass",
            f"{d['service']} {d.get('version')} {d['minutes_before_onset']} min before onset",
            [ev],
        )
    return _result(
        name, "fail", "no deployment of the subject in the 30 min before onset", f.deploy_ids
    )


def symptoms_after_deploy(f: Facts, subj: set[str]) -> CheckResult:
    name = "symptoms_start_after_deployment"
    deps = [(ev, d) for s in sorted(subj) for ev, d in f.deployments.get(s, []) if d.get("at")]
    starts = [(t, ev) for s in subj for t, ev in [f.first_symptom(s)] if t is not None]
    if not deps or not starts:
        return _result(name, "not_run", "deployment time or symptom onset unknown")
    ev_d, dep = max(deps, key=lambda x: x[1]["at"])
    t_subj, ev_s = min(starts)
    res: Result = "pass" if _t(dep["at"]) <= t_subj else "fail"
    return _result(name, res, f"deployed {dep['at']}, symptoms {t_subj.isoformat()}", [ev_d, ev_s])


def nothing_changed_first(f: Facts, subj: set[str]) -> CheckResult:
    name = "no_other_service_changed_first"
    deps = [d for s in subj for _, d in f.deployments.get(s, []) if d.get("at")]
    if not deps:
        return _result(name, "not_run", "no deployment of the subject to compare against")
    t_dep = max(_t(d["at"]) for d in deps)
    earlier = [x for x in f.symptoms_of_others(subj) if x[0] < t_dep]
    if earlier:
        _, svc, ev = earlier[0]
        return _result(name, "fail", f"{svc} had symptoms before the deployment", [ev])
    return _result(
        name, "pass", "no other service had symptoms before the deployment", f.detection_ids
    )


def monotonic_increase(f: Facts, subj: set[str]) -> CheckResult:
    # Needs per-point series slope; metric_query stores window aggregates only. Not collected in
    # Phase 4, so this check never passes and a leak is never confirmed (stated, not guessed).
    return _result("monotonic_increase_before_onset", "not_run", "series slope not collected")


def restarts_if_present(f: Facts, subj: set[str]) -> CheckResult:
    name = "restarts_or_oom_if_present"
    hc = [d for d in f.detections if d.get("service") in subj and d.get("rule") == "health_check"]
    if hc:
        return _result(name, "pass", "restart/heartbeat detection on the subject", [hc[0]["_ev"]])
    return _result(name, "not_applicable", "no restart or OOM signal (optional)")


def propagated_from_callee(f: Facts, subj: set[str]) -> CheckResult:
    name = "subject_errors_explained_by_erroring_callee"
    erroring = [(ev, p) for ev, p in f.paths if p.get("reason") == "erroring" and p.get("path")]
    if not erroring:
        return _result(name, "not_run", "no erroring traces collected")
    for ev, p in erroring:
        chain = p["path"]
        for i, span in enumerate(chain):
            if span.get("service") in subj and span.get("error"):
                below = [
                    s for s in chain[i + 1 :] if s.get("service") not in subj and s.get("error")
                ]
                if below:
                    return _result(
                        name, "pass", f"error below the subject in {below[0]['service']}", [ev]
                    )
    return _result(
        name, "fail", "no erroring callee under the subject's errors", [ev for ev, _ in erroring]
    )


# Keyed by the taxonomy's check names (a unit test proves every taxonomy check is implemented).
CHECKS: dict[str, Check] = {
    "saturation_or_timeout_signature_in_subject": saturation_signature,
    "subject_onset_not_after_first_downstream_symptom": subject_first,
    "subject_outbound_or_db_error_rate_rose": outbound_errors_rose,
    "operation_latency_rose": latency_rose,
    "operation_self_time_dominates_critical_path_increase": self_time_dominates,
    "no_earlier_error_rate_change_elsewhere": no_earlier_errors_elsewhere,
    "connection_errors_on_edge_to_target": edge_errors_to_target,
    "target_inbound_traffic_dropped_or_errored": target_inbound_degraded,
    "onset_order_consistent": onset_order,
    "subject_deployed_within_30_min_before_onset": recent_deploy,
    "symptoms_start_after_deployment": symptoms_after_deploy,
    "no_other_service_changed_first": nothing_changed_first,
    "monotonic_increase_before_onset": monotonic_increase,
    "restarts_or_oom_if_present": restarts_if_present,
    "subject_errors_explained_by_erroring_callee": propagated_from_callee,
}


def verify(
    *,
    category: str,
    subject_names: set[str],
    supporting: Sequence[str],
    contradicting: Sequence[str],
    evidence: Sequence[EvidenceView],
) -> Verdict:
    """Citation check, then category checks, then status. Deterministic."""
    known = {e.id for e in evidence}
    sup = tuple(i for i in dict.fromkeys(supporting) if i in known)
    con = tuple(i for i in dict.fromkeys(contradicting) if i in known)
    rejected = tuple(sorted({*supporting, *contradicting} - known))
    spec = TAXONOMY.get(category)
    facts = Facts.build(evidence)
    checks = tuple(CHECKS[name](facts, subject_names) for name in (spec.checks if spec else ()))
    passed = sum(c.result == "pass" for c in checks)
    applicable = [c for c in checks if c.result != "not_applicable"]
    if not sup:
        status: Status = "inconclusive"
        reason = "no valid supporting citation"
    elif not applicable or passed == 0:
        status = "inconclusive"
        reason = "no category check passed" if applicable else "category has no checks"
    elif all(c.result == "pass" for c in applicable) and not con:
        status = "confirmed"
        reason = "all category checks passed on direct evidence; no contradicting evidence"
    else:
        status = "probable"
        reason = (
            "contradicting evidence cited" if con else f"{passed}/{len(applicable)} checks passed"
        )
    return Verdict(status, checks, sup, con, rejected, reason)


def rank(verdicts: Sequence[tuple[int, Verdict]]) -> list[int]:
    """Order of hypotheses (by proposed index): status, checks passed, supporting evidence count,
    then the analyst's proposed order as the final tie-break. Confidence plays no part."""
    return [
        i
        for i, _ in sorted(
            verdicts,
            key=lambda iv: (
                STATUS_ORDER[iv[1].status],
                -iv[1].passed,
                -len(iv[1].supporting),
                iv[0],
            ),
        )
    ]
