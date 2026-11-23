"""Runs one investigation: lease → graph (fresh or resumed from its checkpoint) → final status.

Crash resume (plan §7): the Celery task is `acks_late` + `reject_on_worker_lost`, so a killed
worker's task is redelivered. The new run takes the lease once the old one has expired (or at
once if the old run released it while failing) and continues from the last checkpoint of
`thread_id` = investigation id; re-executed nodes find their evidence through the dedup key.
Two workers never run the same investigation: the lease is a conditional UPDATE, renewed every
15 s, and a run that loses it stops.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphRecursionError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.core.tenancy import SystemScope
from app.investigation.controls import BudgetExceededError, CancelRequestedError, Guard
from app.investigation.events import Events, emit_in
from app.investigation.graph import EngineDeps, Hook, InvestigationState, build_graph
from app.llm.client import LLMClient
from app.llm.types import CallRecord
from app.repositories.investigations import (
    InvestigationDeadLetterRepository,
    InvestigationRepository,
)
from app.repositories.llm_calls import LLMCallRepository

logger = get_logger("app.investigation.runner")

LEASE_TTL = timedelta(seconds=60)
RENEW_EVERY_S = 15.0
POLL_EVERY_S = 1.0  # cancellation target: stopped within ~1 s (plan: ≤ 5 s)
MAX_RUN_ATTEMPTS = 5  # leases taken; beyond this the run is dead-lettered
GRAPH_RECURSION_BACKSTOP = 100  # the steps budget (Guard) stops first

_TRACING_VARS = (
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING",
    "LANGCHAIN_TRACING_V2",
)


class ExternalTracingEnabledError(RuntimeError):
    """LangSmith/LangChain tracing would send graph state (evidence summaries) to a third party."""


def assert_no_external_tracing(env: Mapping[str, str] | None = None) -> None:
    env = os.environ if env is None else env
    on = [v for v in _TRACING_VARS if env.get(v, "").strip().lower() in {"1", "true", "yes"}]
    if on:
        raise ExternalTracingEnabledError(f"external tracing must be off: {', '.join(on)}")


class CommittingUsageSink:
    """`UsageSink` that commits each `llm_calls` row at once, in its own session, so usage is
    kept even when the node that made the call later crashes."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], scope: SystemScope) -> None:
        self._sm = sessionmaker
        self._scope = scope

    async def record(self, record: CallRecord) -> None:
        async with self._sm() as s:
            await LLMCallRepository(s, self._scope).record(record)
            if record.investigation_id is not None:  # usage only, never text (plan §8)
                await emit_in(
                    s,
                    self._scope,
                    record.investigation_id,
                    "llm_call",
                    node=record.node,
                    purpose=record.purpose,
                    provider=record.provider,
                    model=record.model,
                    attempt=record.attempt,
                    outcome=record.outcome,
                    input_tokens=record.usage.input_tokens,
                    output_tokens=record.usage.output_tokens,
                    latency_ms=record.latency_ms,
                    cost_usd=record.cost_usd,
                )
            await s.commit()


@dataclass
class RunOutcome:
    status: str  # final status, or "skipped"
    reasons: list[str] = field(default_factory=list)
    resumed: bool = False
    skipped: str | None = None
    state: dict[str, Any] = field(default_factory=dict)


async def run_investigation(
    *,
    org_id: uuid.UUID,
    investigation_id: uuid.UUID,
    sessionmaker: async_sessionmaker[AsyncSession],
    llm: LLMClient,
    checkpointer: BaseCheckpointSaver[Any],
    owner: str,
    hooks: Mapping[str, Hook] | None = None,
    step_limit: int = GRAPH_RECURSION_BACKSTOP,
    renew_every_s: float = RENEW_EVERY_S,
    poll_every_s: float = POLL_EVERY_S,
) -> RunOutcome:
    assert_no_external_tracing()
    scope = SystemScope(org_id, "investigation")
    async with sessionmaker() as s:
        repo = InvestigationRepository(s, scope)
        if await repo.cancel_queued(investigation_id):  # cancelled before any worker took it
            await emit_in(
                s,
                scope,
                investigation_id,
                "status_changed",
                status="cancelled",
                reasons=["cancelled"],
            )
            await s.commit()
            return RunOutcome(status="cancelled", reasons=["cancelled"])
        inv = await repo.try_lease(investigation_id, owner, LEASE_TTL)
        await s.commit()
    if inv is None:
        return RunOutcome(status="skipped", skipped="not_leased_or_not_active")
    events = Events(sessionmaker, scope, investigation_id)
    await events.emit("status_changed", status="running", reasons=[])
    if inv.attempts > MAX_RUN_ATTEMPTS:  # keeps dying (e.g. OOM-killed): stop, do not loop
        await dead_letter(sessionmaker, scope, investigation_id, "too_many_attempts", inv.attempts)
        return RunOutcome(status="failed", reasons=["too_many_attempts"])

    guard = Guard(sessionmaker, scope, investigation_id, events=events)
    deps = EngineDeps(
        sessionmaker=sessionmaker, llm=llm, hooks=hooks or {}, guard=guard, events=events
    )
    graph = build_graph(deps).compile(checkpointer=checkpointer)
    config: Any = {
        "configurable": {"thread_id": str(investigation_id)},
        "recursion_limit": step_limit,
    }
    initial: InvestigationState = {
        "investigation_id": str(investigation_id),
        "org_id": str(org_id),
        "incident_id": str(inv.incident_id),
    }

    async def drive() -> tuple[dict[str, Any], bool]:
        snap = await graph.aget_state(config)
        if not snap.values:
            return dict(await graph.ainvoke(initial, config)), False
        if snap.next:
            return dict(await graph.ainvoke(None, config)), True
        return dict(snap.values), True  # finished before; only the status write was lost

    stop: dict[str, str] = {}

    async def watch(main: asyncio.Task[tuple[dict[str, Any], bool]]) -> None:
        """Every `poll_every_s`: cancellation / wall-clock (abandons an in-flight node or LLM
        call at once); every `renew_every_s`: the lease. A lost lease stops this run."""
        loop = asyncio.get_running_loop()
        next_renew = loop.time() + renew_every_s
        while not main.done():
            await asyncio.sleep(poll_every_s)
            reason = await guard.stop_reason()
            if reason is not None:
                stop["reason"] = reason
                main.cancel()
                return
            if loop.time() >= next_renew:
                next_renew = loop.time() + renew_every_s
                async with sessionmaker() as s:
                    ok = await InvestigationRepository(s, scope).renew_lease(
                        investigation_id, owner, LEASE_TTL
                    )
                    await s.commit()
                if not ok:
                    logger.warning(
                        "investigation_lease_lost", investigation_id=str(investigation_id)
                    )
                    stop["reason"] = "lease_lost"
                    main.cancel()
                    return

    async def state_reasons() -> tuple[dict[str, Any], list[str]]:
        values = dict((await graph.aget_state(config)).values)
        return values, list(values.get("incomplete_reasons", []))

    main = asyncio.create_task(drive())
    watcher = asyncio.create_task(watch(main))
    resumed = False
    try:
        try:
            final, resumed = await main
            reasons = list(final.get("incomplete_reasons", []))
            status = "incomplete" if reasons else "completed"
        except BudgetExceededError as exc:
            final, reasons = await state_reasons()
            status, reasons = "incomplete", [*reasons, f"budget:{exc.budget}"]
        except CancelRequestedError:
            final, reasons = await state_reasons()
            status, reasons = "cancelled", [*reasons, "cancelled"]
        except GraphRecursionError:  # backstop; the steps budget normally stops first
            final, reasons = await state_reasons()
            status, reasons = "incomplete", [*reasons, "budget:steps"]
        except asyncio.CancelledError:
            why = stop.get("reason")
            if why is None:
                raise  # this coroutine itself was cancelled: not ours to finish
            if why == "lease_lost":
                return RunOutcome(status="skipped", skipped="lease_lost")
            final, reasons = await state_reasons()
            if why == "cancel":
                status, reasons = "cancelled", [*reasons, "cancelled"]
            else:
                status, reasons = "incomplete", [*reasons, why]
        async with sessionmaker() as s:
            if await InvestigationRepository(s, scope).finish(
                investigation_id, owner, status, reasons
            ):
                await emit_in(
                    s, scope, investigation_id, "status_changed", status=status, reasons=reasons
                )
            await s.commit()
        logger.info(
            "investigation_finished",
            investigation_id=str(investigation_id),
            status=status,
            reasons=reasons,
            resumed=resumed,
        )
        return RunOutcome(status=status, reasons=reasons, resumed=resumed, state=final)
    except BaseException:
        # Leave the run `running` and resumable at once (a SIGKILL instead lets the lease expire).
        async with sessionmaker() as s:
            await asyncio.shield(
                InvestigationRepository(s, scope).release_lease(investigation_id, owner)
            )
            await s.commit()
        raise
    finally:
        watcher.cancel()


async def dead_letter(
    sessionmaker: async_sessionmaker[AsyncSession],
    scope: SystemScope,
    investigation_id: uuid.UUID,
    error_class: str,
    attempts: int,
) -> None:
    """Record the dead letter and set `failed` (plan §7). Idempotent."""
    async with sessionmaker() as s:
        repo = InvestigationRepository(s, scope)
        inv = await repo.get(investigation_id)
        last_node = inv.last_node if inv is not None else None
        await InvestigationDeadLetterRepository(s, scope).record_once(
            investigation_id, error_class, attempts, last_node
        )
        if await repo.fail(investigation_id, [f"failed:{error_class}"]):
            await emit_in(
                s,
                scope,
                investigation_id,
                "status_changed",
                status="failed",
                reasons=[f"failed:{error_class}"],
            )
        await s.commit()
    logger.error(
        "investigation_dead_lettered",
        investigation_id=str(investigation_id),
        error_class=error_class,
        attempts=attempts,
        last_node=last_node,
    )
