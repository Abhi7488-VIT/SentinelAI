"""The investigation workflow as a LangGraph state graph (plan §5).

    planner ─┬─> telemetry ──┐
             └─> dependency ─┴─> analyst ─> verification ─> report

- State holds only small values (ids, names, window, findings text, hypotheses), never raw
  telemetry: evidence content lives in the `evidence` table. Checkpoints stay small.
- Every node is safe to re-run after a crash: tools deduplicate evidence by `(tool, params)` and
  each tool's evidence is committed as soon as it is recorded.
- Tools are fixed per node in code. The LLM only chooses ids from enumerated sets; anything else
  is dropped and recorded in `rejected` (T6 turns these into `rejected_by_allowlist` events).
- Every id an LLM cites is checked against the `evidence` table for this investigation and org;
  unknown ids are removed and recorded (invariant 1).
- Verification (T4) is deterministic (`app.investigation.verification`): it alone sets hypothesis
  status, rank and primary. The report is a placeholder until T7 (`report_not_generated`), so a
  run still ends `incomplete`, never `completed` (invariant 2).
"""

from __future__ import annotations

import operator
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.core.tenancy import SystemScope
from app.investigation.controls import BudgetExceededError, Guard
from app.investigation.prompting import (
    claims_block,
    escape,
    evidence_blocks,
    services_block,
)
from app.investigation.schemas import AnalystOutput, InvestigatorFindings, PlannerOutput
from app.investigation.taxonomy import describe as describe_taxonomy
from app.investigation.tools import (
    ToolContext,
    dependency_lookup,
    deploy_lookup,
    detection_ref,
    log_query,
    metric_query,
    trace_analysis,
)
from app.investigation.verification import EvidenceView, verify
from app.investigation.verification import rank as rank_verdicts
from app.llm.client import LLMClient
from app.llm.errors import (
    LLMBudgetExceededError,
    LLMError,
    LLMFatalError,
    LLMInputTooLargeError,
    LLMInvalidOutputError,
    LLMRefusalError,
    LLMTimeoutError,
    LLMTransportError,
)
from app.llm.prompts import Prompt, load_prompt
from app.llm.types import CallContext, LLMRequest, Purpose
from app.models.investigation import Evidence
from app.repositories.incidents import IncidentRepository, IncidentServiceRepository
from app.repositories.investigations import (
    EvidenceRepository,
    HypothesisRepository,
    HypothesisWrite,
)
from app.repositories.service_graph import ServiceEdgeRepository
from app.repositories.services import ServiceRepository

logger = get_logger("app.investigation.graph")

MAX_FOCUS = 5
MAX_CANDIDATES = 20
PRE_ONSET = timedelta(minutes=1)
POST_ACTIVITY = timedelta(minutes=15)
BASELINE = timedelta(minutes=30)
MAX_WINDOW = timedelta(hours=6)
PROMPT_VERSION = 1


def merge_unique(left: list[str] | None, right: list[str] | None) -> list[str]:
    """List reducer that keeps first-seen order and drops repeats (fan-in from branches)."""
    return list(dict.fromkeys([*(left or []), *(right or [])]))


class InvestigationState(TypedDict, total=False):
    investigation_id: str
    org_id: str
    incident_id: str
    window: dict[str, str]
    candidates: dict[str, str]
    focus: list[str]
    plan_source: str
    evidence_ids: Annotated[list[str], merge_unique]
    findings: Annotated[list[dict[str, Any]], operator.add]
    hypotheses: list[dict[str, Any]]
    rejected: Annotated[list[dict[str, Any]], operator.add]
    incomplete_reasons: Annotated[list[str], merge_unique]
    stages: Annotated[list[str], operator.add]


Hook = Callable[[], Awaitable[None]]


class NodeFn(Protocol):
    """A node as LangGraph types it (the parameter must be named `state`)."""

    def __call__(self, state: InvestigationState) -> Awaitable[dict[str, Any]]: ...


@dataclass
class EngineDeps:
    sessionmaker: async_sessionmaker[AsyncSession]
    llm: LLMClient
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    # Test seams: named points inside nodes (e.g. "telemetry.after_first_tool").
    hooks: Mapping[str, Hook] = field(default_factory=dict)
    # Budgets + cancellation (T5); None only in tests that exercise the graph alone.
    guard: Guard | None = None

    async def hook(self, name: str) -> None:
        fn = self.hooks.get(name)
        if fn is not None:
            await fn()


@dataclass(frozen=True, slots=True)
class Windows:
    onset: datetime
    start: datetime
    end: datetime
    baseline_start: datetime
    baseline_end: datetime

    def to_state(self) -> dict[str, str]:
        return {k: getattr(self, k).isoformat() for k in self.__slots__}

    @classmethod
    def from_state(cls, raw: Mapping[str, str]) -> Windows:
        return cls(**{k: datetime.fromisoformat(raw[k]) for k in cls.__slots__})


def compute_windows(started_at: datetime, last_activity_at: datetime, now: datetime) -> Windows:
    """Incident window [onset - 1 min, min(now, last activity + 15 min)] (≤ 6 h) and the 30-min
    baseline right before it. Deterministic; the LLM never chooses times."""
    onset = started_at
    start = onset - PRE_ONSET
    end = min(now, max(last_activity_at, onset) + POST_ACTIVITY)
    end = max(end, start + timedelta(minutes=1))
    end = min(end, start + MAX_WINDOW)
    return Windows(onset, start, end, start - BASELINE, start)


_LLM_REASON: Sequence[tuple[type[LLMError], str]] = (
    (LLMInvalidOutputError, "llm_invalid_output"),
    (LLMTimeoutError, "llm_timeout"),
    (LLMTransportError, "llm_transport_error"),
    (LLMRefusalError, "llm_refusal"),
    (LLMInputTooLargeError, "llm_input_too_large"),
    (LLMFatalError, "llm_fatal_error"),
)


def llm_reason(exc: LLMError, node: str) -> str:
    for cls, name in _LLM_REASON:
        if isinstance(exc, cls):
            return f"{name}:{node}"
    return f"llm_error:{node}"


def _ids(state: InvestigationState) -> tuple[uuid.UUID, SystemScope]:
    return uuid.UUID(state["investigation_id"]), SystemScope(
        uuid.UUID(state["org_id"]), "investigation"
    )


def build_graph(deps: EngineDeps) -> StateGraph[InvestigationState]:
    async def tool[**P](
        fn: Callable[P, Awaitable[Evidence]], *args: P.args, **kw: P.kwargs
    ) -> Evidence:
        """Every tool call is counted and checked against budgets/cancellation first."""
        if deps.guard is not None:
            await deps.guard.before_tool()
        return await fn(*args, **kw)

    async def call_llm[T: BaseModel](
        state: InvestigationState,
        node: str,
        purpose: Purpose,
        prompt: Prompt,
        blocks: Sequence[str],
        model: type[T],
        system: str | None = None,
    ) -> tuple[T | None, str | None]:
        inv_id, scope = _ids(state)
        request = LLMRequest(
            purpose=purpose,
            prompt_id=prompt.id,
            prompt_version=prompt.version,
            system=system or prompt.text,
            user_blocks=tuple(blocks),
            output_model=model,
        )
        try:
            result = await deps.llm.generate(
                request,
                CallContext(
                    org_id=scope.org_id,
                    investigation_id=inv_id,
                    node=node,
                    pre_call=deps.guard.before_llm if deps.guard is not None else None,
                ),
            )
        except LLMBudgetExceededError as exc:
            raise BudgetExceededError(exc.budget) from None  # stops the run, not just this call
        except LLMError as exc:
            logger.warning("investigation_llm_failed", node=node, error=type(exc).__name__)
            return None, llm_reason(exc, node)
        return result.parsed, None

    async def check_citations(
        state: InvestigationState, node: str, ids: Sequence[str]
    ) -> tuple[set[str], list[dict[str, Any]]]:
        inv_id, scope = _ids(state)
        async with deps.sessionmaker() as s:
            valid = await EvidenceRepository(s, scope).existing_ids(inv_id, list(ids))
        rejected = [
            {"node": node, "kind": "citation", "value": i[:40]}
            for i in dict.fromkeys(ids)
            if i not in valid
        ]
        return valid, rejected

    async def findings_from(
        state: InvestigationState, node: str, out: InvestigatorFindings
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        valid, rejected = await check_citations(
            state, node, [i for c in out.claims for i in c.evidence_ids]
        )
        findings = []
        for claim in out.claims:
            ids = [i for i in dict.fromkeys(claim.evidence_ids) if i in valid]
            if ids:  # a claim with no resolvable citation is not kept (invariant 1)
                findings.append({"node": node, "statement": claim.statement, "evidence_ids": ids})
        return findings, rejected

    async def planner(state: InvestigationState) -> dict[str, Any]:
        inv_id, scope = _ids(state)
        incident_id = uuid.UUID(state["incident_id"])
        async with deps.sessionmaker() as s:
            ctx = await ToolContext.open(s, scope, inv_id, "planner")
            incident = await IncidentRepository(s, scope).get(incident_id)
            if incident is None:
                return {"incomplete_reasons": ["incident_not_found"], "stages": ["planner"]}
            affected = (await IncidentServiceRepository(s, scope).of([incident_id]))[incident_id]
            affected = affected[:MAX_CANDIDATES]
            w = compute_windows(incident.started_at, incident.last_activity_at, deps.now())
            evidence: list[Evidence] = [await tool(detection_ref, ctx, incident_id=incident_id)]
            await s.commit()
            if not affected:
                return {
                    "window": w.to_state(),
                    "evidence_ids": [evidence[0].id],
                    "incomplete_reasons": ["no_affected_services"],
                    "stages": ["planner"],
                }
            evidence.append(
                await tool(
                    dependency_lookup,
                    ctx,
                    service_ids=affected,
                    start=w.start,
                    end=w.end,
                    baseline_start=w.baseline_start,
                    baseline_end=w.baseline_end,
                )
            )
            await s.commit()
            evidence.append(
                await tool(
                    deploy_lookup,
                    ctx,
                    service_ids=affected,
                    start=w.start,
                    end=w.end,
                    onset=w.onset,
                )
            )
            await s.commit()
            neighbours: list[uuid.UUID] = []
            edges = ServiceEdgeRepository(s, scope)
            for svc in affected:
                neighbours.extend(sorted(await edges.neighbors(svc), key=str))
            ordered = list(dict.fromkeys([*affected, *neighbours]))[:MAX_CANDIDATES]
            found = {x.id: x.name for x in await ServiceRepository(s, scope).get_many(ordered)}
        candidates = {str(i): found[i] for i in ordered if i in found}
        affected_names = {str(i): found[i] for i in affected if i in found}

        blocks = [
            services_block("Affected services (from the incident)", affected_names),
            services_block("Candidate services (choose focus ids only from these)", candidates),
            evidence_blocks(evidence, with_data=True)[0],
        ]
        out, reason = await call_llm(
            state,
            "planner",
            "planner",
            load_prompt("planner", PROMPT_VERSION),
            blocks,
            PlannerOutput,
        )
        rejected: list[dict[str, Any]] = []
        focus: list[str] = []
        if out is not None:
            for sid in out.focus_service_ids:
                if sid in candidates:
                    focus.append(sid)
                else:
                    rejected.append({"node": "planner", "kind": "service", "value": sid[:40]})
            focus = list(dict.fromkeys(focus))[:MAX_FOCUS]
        plan_source = "llm"
        if not focus:  # LLM failed or chose nothing valid: deterministic, labelled fallback
            focus = [str(i) for i in affected if str(i) in candidates][:MAX_FOCUS]
            plan_source = f"fallback:{reason or 'no_valid_focus'}"
        return {
            "window": w.to_state(),
            "candidates": candidates,
            "focus": focus,
            "plan_source": plan_source,
            "evidence_ids": [e.id for e in evidence],
            "rejected": rejected,
            "stages": ["planner"],
        }

    def _focus(state: InvestigationState) -> list[uuid.UUID]:
        return [uuid.UUID(i) for i in state.get("focus", [])]

    async def telemetry(state: InvestigationState) -> dict[str, Any]:
        await deps.hook("telemetry.start")
        focus = _focus(state)
        if not focus:
            return {"stages": ["telemetry"]}
        inv_id, scope = _ids(state)
        w = Windows.from_state(state["window"])
        names = state.get("candidates", {})
        evidence: list[Evidence] = []
        missing: list[str] = []
        async with deps.sessionmaker() as s:
            ctx = await ToolContext.open(s, scope, inv_id, "telemetry")
            for svc in focus:
                name = names.get(str(svc), str(svc))
                logs = await tool(log_query, ctx, service_id=svc, start=w.start, end=w.end)
                await s.commit()
                await deps.hook("telemetry.after_first_tool")
                metrics = await tool(
                    metric_query,
                    ctx,
                    service_id=svc,
                    start=w.start,
                    end=w.end,
                    baseline_start=w.baseline_start,
                    baseline_end=w.baseline_end,
                )
                await s.commit()
                traces = await tool(
                    trace_analysis,
                    ctx,
                    service_ids=[svc],
                    start=w.start,
                    end=w.end,
                    baseline_start=w.baseline_start,
                    baseline_end=w.baseline_end,
                )
                await s.commit()
                evidence += [logs, metrics, traces]
                if logs.row_count == 0:
                    missing.append(f"no_telemetry:{name}:logs")
                if not metrics.data.get("series"):
                    missing.append(f"no_telemetry:{name}:metrics")
                if traces.row_count == 0:
                    missing.append(f"no_telemetry:{name}:traces")
            evidence.append(
                await tool(
                    deploy_lookup, ctx, service_ids=focus, start=w.start, end=w.end, onset=w.onset
                )
            )
            await s.commit()
        if len(missing) == 3 * len(focus):
            missing.append("no_telemetry:all")
        text, left_out = evidence_blocks(evidence, with_data=True)
        blocks = [
            services_block("Focus services", {str(f): names.get(str(f), "?") for f in focus}),
            text,
        ]
        out, reason = await call_llm(
            state,
            "telemetry",
            "telemetry",
            load_prompt("telemetry", PROMPT_VERSION),
            blocks,
            InvestigatorFindings,
        )
        findings: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        if out is not None:
            findings, rejected = await findings_from(state, "telemetry", out)
        return {
            "evidence_ids": [e.id for e in evidence],
            "findings": findings,
            "rejected": rejected,
            "incomplete_reasons": missing
            + ([reason] if reason else [])
            + ([f"evidence_not_shown:telemetry:{len(left_out)}"] if left_out else []),
            "stages": ["telemetry"],
        }

    async def dependency(state: InvestigationState) -> dict[str, Any]:
        await deps.hook("dependency.start")
        focus = _focus(state)
        if not focus:
            return {"stages": ["dependency"]}
        inv_id, scope = _ids(state)
        w = Windows.from_state(state["window"])
        names = state.get("candidates", {})
        async with deps.sessionmaker() as s:
            ctx = await ToolContext.open(s, scope, inv_id, "dependency")
            edges = await tool(
                dependency_lookup,
                ctx,
                service_ids=focus,
                start=w.start,
                end=w.end,
                baseline_start=w.baseline_start,
                baseline_end=w.baseline_end,
            )
            await s.commit()
            traces = await tool(
                trace_analysis,
                ctx,
                service_ids=focus,
                start=w.start,
                end=w.end,
                baseline_start=w.baseline_start,
                baseline_end=w.baseline_end,
            )
            await s.commit()
        evidence = [edges, traces]
        text, _ = evidence_blocks(evidence, with_data=True)
        blocks = [
            services_block("Focus services", {str(f): names.get(str(f), "?") for f in focus}),
            text,
        ]
        out, reason = await call_llm(
            state,
            "dependency",
            "dependency",
            load_prompt("dependency", PROMPT_VERSION),
            blocks,
            InvestigatorFindings,
        )
        findings: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        if out is not None:
            findings, rejected = await findings_from(state, "dependency", out)
        await deps.hook("dependency.end")
        return {
            "evidence_ids": [e.id for e in evidence],
            "findings": findings,
            "rejected": rejected,
            "incomplete_reasons": [reason] if reason else [],
            "stages": ["dependency"],
        }

    async def analyst(state: InvestigationState) -> dict[str, Any]:
        inv_id, scope = _ids(state)
        candidates = state.get("candidates", {})
        if not state.get("findings") and not state.get("evidence_ids"):
            return {"hypotheses": [], "incomplete_reasons": ["no_evidence"], "stages": ["analyst"]}
        async with deps.sessionmaker() as s:
            evidence = await EvidenceRepository(s, scope).many(
                inv_id, state.get("evidence_ids", [])
            )
        text, left_out = evidence_blocks(evidence, with_data=False)
        prompt = load_prompt("analyst", PROMPT_VERSION)
        blocks = [
            services_block("Candidate services (subject ids only from these)", candidates),
            claims_block(state.get("findings", [])),
            text,
        ]
        out, reason = await call_llm(
            state,
            "analyst",
            "analyst",
            prompt,
            blocks,
            AnalystOutput,
            system=prompt.text.replace("{taxonomy}", escape(describe_taxonomy())),
        )
        if out is None:
            return {
                "hypotheses": [],
                "incomplete_reasons": [reason or "llm_error:analyst"],
                "stages": ["analyst"],
            }
        cited = [
            i
            for h in out.hypotheses
            for i in (*h.supporting_evidence_ids, *h.contradicting_evidence_ids)
        ]
        valid, rejected = await check_citations(state, "analyst", cited)
        hypotheses: list[dict[str, Any]] = []
        for rank, h in enumerate(out.hypotheses):
            subjects = [i for i in dict.fromkeys(h.subject_service_ids) if i in candidates]
            rejected += [
                {"node": "analyst", "kind": "service", "value": i[:40]}
                for i in h.subject_service_ids
                if i not in candidates
            ]
            if not subjects:
                continue
            hypotheses.append(
                {
                    "proposed_rank": rank,
                    "category": h.category,
                    "subject_service_ids": subjects,
                    "statement": h.statement,
                    "supporting_evidence_ids": [
                        i for i in dict.fromkeys(h.supporting_evidence_ids) if i in valid
                    ],
                    "contradicting_evidence_ids": [
                        i for i in dict.fromkeys(h.contradicting_evidence_ids) if i in valid
                    ],
                    "rejected_citations": sorted(
                        {
                            i
                            for i in (*h.supporting_evidence_ids, *h.contradicting_evidence_ids)
                            if i not in valid
                        }
                    ),
                    "missing_evidence": h.missing_evidence,
                    "model_confidence": h.confidence,  # display only, never decides status
                }
            )
        return {
            "hypotheses": hypotheses,
            "rejected": rejected,
            "incomplete_reasons": [f"evidence_not_shown:analyst:{len(left_out)}"]
            if left_out
            else [],
            "stages": ["analyst"],
        }

    async def verification(state: InvestigationState) -> dict[str, Any]:
        """Deterministic: citation check, category checks, status, ranking (plan §6). Persists
        hypotheses, citations and check results; a re-run replaces them (same inputs, same rows)."""
        inv_id, scope = _ids(state)
        candidates = state.get("candidates", {})
        drafts = state.get("hypotheses", [])
        async with deps.sessionmaker() as s:
            rows = await EvidenceRepository(s, scope).many(inv_id, state.get("evidence_ids", []))
            views = [EvidenceView(e.id, e.kind, e.data) for e in rows]
            verdicts = [
                (
                    i,
                    verify(
                        category=h["category"],
                        subject_names={
                            candidates[x] for x in h["subject_service_ids"] if x in candidates
                        },
                        supporting=h["supporting_evidence_ids"],
                        contradicting=h["contradicting_evidence_ids"],
                        evidence=views,
                    ),
                )
                for i, h in enumerate(drafts)
            ]
            order = rank_verdicts(verdicts)
            by_index = dict(verdicts)
            primary = order[0] if order and by_index[order[0]].status != "inconclusive" else None
            writes = []
            verified: list[dict[str, Any]] = []
            for position, i in enumerate(order, start=1):
                h, v = drafts[i], by_index[i]
                rejected = sorted({*h.get("rejected_citations", []), *v.rejected})
                checks = [(c.name, c.result, c.detail, list(c.evidence_ids)) for c in v.checks]
                writes.append(
                    HypothesisWrite(
                        proposed_rank=i,
                        rank=position,
                        is_primary=i == primary,
                        category=h["category"],
                        subject_service_ids=h["subject_service_ids"],
                        statement=h["statement"],
                        status=v.status,
                        status_reason=v.reason,
                        checks_passed=v.passed,
                        checks_applicable=v.applicable,
                        model_confidence=float(h["model_confidence"]),
                        rejected_citations=rejected,
                        missing_evidence=h.get("missing_evidence", []),
                        supporting=list(v.supporting),
                        contradicting=list(v.contradicting),
                        checks=checks,
                    )
                )
                verified.append(
                    {
                        **h,
                        "supporting_evidence_ids": list(v.supporting),
                        "contradicting_evidence_ids": list(v.contradicting),
                        "rejected_citations": rejected,
                        "status": v.status,
                        "status_reason": v.reason,
                        "rank": position,
                        "is_primary": i == primary,
                        "checks": [
                            {"name": n, "result": r, "detail": d, "evidence_ids": e}
                            for n, r, d, e in checks
                        ],
                    }
                )
            await HypothesisRepository(s, scope).replace_for_investigation(inv_id, writes)
            await s.commit()
        reasons = (
            []
            if drafts
            or any(
                r.startswith("llm_") and r.endswith(":analyst")
                for r in state.get("incomplete_reasons", [])
            )
            else ["no_hypotheses"]
        )
        return {"hypotheses": verified, "incomplete_reasons": reasons, "stages": ["verification"]}

    async def report(state: InvestigationState) -> dict[str, Any]:
        # T7 replaces this with the report builder and its citation gate.
        return {"incomplete_reasons": ["report_not_generated"], "stages": ["report"]}

    def guarded(name: str, fn: NodeFn) -> NodeFn:
        async def run(state: InvestigationState) -> dict[str, Any]:
            if deps.guard is not None:
                await deps.guard.enter(name)  # cancellation, wall-clock, steps; counts the step
            return await fn(state)

        return run

    g: StateGraph[InvestigationState] = StateGraph(InvestigationState)
    for name, fn in (
        ("planner", planner),
        ("telemetry", telemetry),
        ("dependency", dependency),
        ("analyst", analyst),
        ("verification", verification),
        ("report", report),
    ):
        g.add_node(name, guarded(name, fn))
    g.add_edge(START, "planner")
    g.add_edge("planner", "telemetry")
    g.add_edge("planner", "dependency")
    g.add_edge(["telemetry", "dependency"], "analyst")  # join: waits for both branches
    g.add_edge("analyst", "verification")
    g.add_edge("verification", "report")
    g.add_edge("report", END)
    return g


CompiledGraph = CompiledStateGraph[InvestigationState]
