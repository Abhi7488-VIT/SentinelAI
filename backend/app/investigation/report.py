"""Investigation report (Phase 4 plan §8, decision D8): 14 sections assembled from stored rows.

Every number and table comes from the database (evidence data, verification rows, `llm_calls`);
nothing is estimated. The summary is a deterministic template over the verified primary
hypothesis (no LLM call: fewer tokens, nothing to verify). Model-written text that appears
(hypothesis statements, findings) is labelled as such.

Citation gate: every `ev_…` id anywhere in the report must exist in `evidence` for this
investigation. A report with an unresolved id is not stored; the run ends
`incomplete: report_citation_check_failed` (the harness re-checks from outside, §10).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import TenantScope
from app.models.investigation import REPORT_SCHEMA_VERSION, Evidence, Hypothesis
from app.repositories.incidents import IncidentRepository
from app.repositories.investigations import (
    EvidenceRepository,
    HypothesisEvidenceRepository,
    HypothesisRepository,
    InvestigationEventRepository,
    InvestigationRepository,
    VerificationCheckRepository,
)
from app.repositories.llm_calls import LLMCallRepository
from app.repositories.projects import ProjectRepository

EVIDENCE_ID = re.compile(r"ev_[a-z2-7]{12}")
SECTIONS = (
    "summary",
    "status",
    "scope",
    "impact",
    "detection_signals",
    "timeline",
    "primary_candidate",
    "alternatives",
    "supporting_evidence",
    "contradicting_evidence",
    "verification",
    "gaps",
    "next_steps",
    "run_metadata",
)
TERMINAL_STAGE_REASONS = frozenset({"report_not_generated"})


def cited_ids(report: dict[str, Any]) -> list[str]:
    return sorted(set(EVIDENCE_ID.findall(json.dumps(report, ensure_ascii=False))))


def citation_gate(report: dict[str, Any], known: set[str]) -> list[str]:
    """Ids cited anywhere in the report that do not exist for this investigation."""
    return [i for i in cited_ids(report) if i not in known]


def _evidence_ref(e: Evidence) -> dict[str, Any]:
    return {
        "evidence_id": e.id,
        "kind": e.kind,
        "summary": e.summary,
        "time_from": e.time_from.isoformat(),
        "time_to": e.time_to.isoformat(),
        "source_query": e.source_ref,
        "row_count": e.row_count,
        "truncated": e.truncated,
    }


def _hyp_ref(h: Hypothesis, names: dict[str, str]) -> dict[str, Any]:
    return {
        "rank": h.rank,
        "category": h.category,
        "subjects": [names.get(s, s) for s in h.subject_service_ids],
        "status": h.status,
        "status_reason": h.status_reason,
        "checks_passed": h.checks_passed,
        "checks_applicable": h.checks_applicable,
        "statement_by_model": h.statement,
        "model_confidence_display_only": h.model_confidence,
    }


async def build_report(
    session: AsyncSession,
    scope: TenantScope,
    investigation_id: uuid.UUID,
    state: dict[str, Any],
    now: datetime | None = None,
) -> tuple[dict[str, Any], set[str]]:
    """The report and the set of evidence ids that exist for this investigation."""
    now = now or datetime.now(UTC)
    inv = await InvestigationRepository(session, scope).get(investigation_id)
    if inv is None:
        raise LookupError("investigation not found")
    incident = await IncidentRepository(session, scope).get(inv.incident_id)
    project = await ProjectRepository(session, scope).get(incident.project_id) if incident else None
    names: dict[str, str] = dict(state.get("candidates", {}))
    ev_repo = EvidenceRepository(session, scope)
    evidence = await ev_repo.many(investigation_id, state.get("evidence_ids", []))
    by_id = {e.id: e for e in evidence}
    hyps = await HypothesisRepository(session, scope).for_investigation(investigation_id)
    hyp_ids = [h.id for h in hyps]
    cites = await HypothesisEvidenceRepository(session, scope).for_hypotheses(hyp_ids)
    checks = await VerificationCheckRepository(session, scope).for_hypotheses(hyp_ids)
    calls = await LLMCallRepository(session, scope).for_investigation(investigation_id)
    events = await InvestigationEventRepository(session, scope).after(investigation_id, None, 1000)

    support: dict[uuid.UUID, list[str]] = {}
    contra: dict[uuid.UUID, list[str]] = {}
    for c in cites:
        (support if c.role == "supporting" else contra).setdefault(c.hypothesis_id, []).append(
            c.evidence_id
        )
    checks_of: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for chk in checks:
        checks_of.setdefault(chk.hypothesis_id, []).append(
            {
                "check": chk.name,
                "result": chk.result,
                "detail": chk.detail,
                "evidence_ids": chk.evidence_ids,
            }
        )
    primary = next((h for h in hyps if h.is_primary), None)
    reasons = [r for r in state.get("incomplete_reasons", []) if r not in TERMINAL_STAGE_REASONS]

    # 1 summary
    if primary is not None:
        ids = support.get(primary.id, [])
        summary = (
            f"Primary candidate ({primary.status}): {primary.category} in "
            f"{', '.join(names.get(s, s) for s in primary.subject_service_ids)}. "
            f"Verification: {primary.status_reason} ({primary.checks_passed}/"
            f"{primary.checks_applicable} checks passed). Supporting evidence: {', '.join(ids)}."
        )
    else:
        summary = (
            f"No hypothesis could be verified: {len(hyps)} candidate(s), none with a passing "
            "check on direct evidence."
            if hyps
            else "No hypothesis was produced."
        )
    # 4 impact (server operations from trace evidence)
    impact = []
    for e in evidence:
        if e.kind != "trace_analysis":
            continue
        for op in e.data.get("operations", []):
            if op.get("kind") == "server":
                impact.append(
                    {
                        "service": op.get("service"),
                        "operation": op.get("operation"),
                        "error_pct": {
                            "baseline": op["baseline"].get("error_pct"),
                            "incident": op["incident"].get("error_pct"),
                        },
                        "p95_ms": {
                            "baseline": op["baseline"].get("p95_ms"),
                            "incident": op["incident"].get("p95_ms"),
                        },
                        "evidence_id": e.id,
                    }
                )
    # 5 detection signals
    detections = [
        {**d, "evidence_id": e.id}
        for e in evidence
        if e.kind == "detection_ref"
        for d in e.data.get("detections", [])
    ]
    # 6 timeline
    timeline: list[dict[str, Any]] = []
    for e in evidence:
        if e.kind == "deploy_lookup":
            timeline += [
                {
                    "at": d.get("at"),
                    "what": f"deployment of {d.get('service')} "
                    f"{d.get('previous_version') or '?'} -> {d.get('version') or '?'}",
                    "evidence_id": e.id,
                }
                for d in e.data.get("deployments", [])
            ]
        elif e.kind == "metric_query":
            timeline += [
                {
                    "at": s.get("onset"),
                    "what": f"{e.data.get('service')} {s.get('name')} left its baseline band",
                    "evidence_id": e.id,
                }
                for s in e.data.get("series", [])
                if s.get("onset")
            ]
    timeline += [
        {
            "at": d.get("started_at"),
            "what": f"{d.get('rule')} detection on {d.get('service')}",
            "evidence_id": d["evidence_id"],
        }
        for d in detections
    ]
    timeline += [
        {"at": ev.created_at.isoformat(), "what": f"investigation: {ev.payload.get('stage')} done"}
        for ev in events
        if ev.type == "stage_completed"
    ]
    timeline = sorted(
        {json.dumps(t, sort_keys=True): t for t in timeline if t.get("at")}.values(),
        key=lambda t: (str(t["at"]), t["what"]),
    )
    # 9/10 evidence
    sup_ids = list(dict.fromkeys(i for h in hyps for i in support.get(h.id, [])))
    con_ids = list(dict.fromkeys(i for h in hyps for i in contra.get(h.id, [])))
    # 13 next steps (suggestions only)
    next_steps = [f"Collect: {m}" for h in hyps for m in (h.missing_evidence or [])] + [
        f"Check '{c['check']}' could not run for {h.category}: {c['detail']}"
        for h in hyps
        for c in checks_of.get(h.id, [])
        if c["result"] == "not_run"
    ]
    # 14 run metadata
    providers = sorted({c.provider for c in calls})
    fake = "fake" in providers
    cost = sum((c.cost_usd or Decimal(0) for c in calls), Decimal(0))
    started = inv.started_at
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "sections": list(SECTIONS),
        "summary": {"text": summary},
        "status": {
            "status": "incomplete" if reasons else "completed",
            "incomplete_reasons": reasons,
            "note": "Status at report time; a budget or cancellation after it is recorded on "
            "the investigation.",
        },
        "scope": {
            "project": project.name if project else None,
            "incident": {
                "number": incident.number if incident else None,
                "title": incident.title if incident else None,
                "started_at": incident.started_at.isoformat() if incident else None,
            },
            "services": sorted(names.values()),
            "focus_services": [names.get(s, s) for s in state.get("focus", [])],
            "plan_source": state.get("plan_source"),
            "window": state.get("window"),
        },
        "impact": impact,
        "detection_signals": detections,
        "timeline": timeline,
        "primary_candidate": (
            {
                **_hyp_ref(primary, names),
                "supporting_evidence_ids": support.get(primary.id, []),
                "checks": checks_of.get(primary.id, []),
            }
            if primary
            else None
        ),
        "alternatives": [
            {
                **_hyp_ref(h, names),
                "why_lower": f"rank {h.rank}: {h.status}, {h.checks_passed}/"
                f"{h.checks_applicable} checks passed",
            }
            for h in hyps
            if h is not primary
        ],
        "supporting_evidence": [_evidence_ref(by_id[i]) for i in sup_ids if i in by_id],
        "contradicting_evidence": [_evidence_ref(by_id[i]) for i in con_ids if i in by_id],
        "verification": [
            {
                "rank": h.rank,
                "category": h.category,
                "status": h.status,
                "checks": checks_of.get(h.id, []),
            }
            for h in hyps
        ],
        "gaps": {
            "incomplete_reasons": reasons,
            # A count, not the ids: they are by definition unresolvable, and every id in a stored
            # report must resolve. The ids stay on the hypothesis rows.
            "rejected_citations": sum(len(h.rejected_citations or []) for h in hyps),
            "rejected_by_allowlist": len(state.get("rejected", [])),
            "budgets_used": {"steps": inv.used_steps, "tool_calls": inv.used_tool_calls},
        },
        "next_steps": {
            "suggestions_only": True,
            "note": "Nothing was executed and no fix was generated (Phase 5).",
            "items": list(dict.fromkeys(next_steps))[:20],
        },
        "run_metadata": {
            "providers": providers,
            "models": sorted({c.model for c in calls}),
            "usage_label": "fake provider counts" if fake else "real provider usage",
            "llm_calls": len(calls),
            "input_tokens": sum(c.input_tokens for c in calls),
            "output_tokens": sum(c.output_tokens for c in calls),
            "cost_usd": None if fake else str(cost),
            "duration_s": round((now - started).total_seconds(), 1) if started else None,
            "budgets": {
                "steps": inv.budget_steps,
                "tokens": inv.budget_tokens,
                "wall_clock_s": inv.budget_wall_clock_s,
                "tool_calls": inv.budget_tool_calls,
                "llm_calls": inv.budget_llm_calls,
                "cost_usd": str(inv.budget_cost_usd),
            },
            "prompt_versions": sorted({f"{c.prompt_id}.v{c.prompt_version}" for c in calls}),
            "evidence_count": len(evidence),
        },
    }
    known = await ev_repo.existing_ids(investigation_id, cited_ids(report))
    return report, known
