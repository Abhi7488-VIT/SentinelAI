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
from app.investigation.graph import EngineDeps, Hook, InvestigationState, build_graph
from app.llm.client import LLMClient
from app.llm.types import CallRecord
from app.repositories.investigations import InvestigationRepository
from app.repositories.llm_calls import LLMCallRepository

logger = get_logger("app.investigation.runner")

LEASE_TTL = timedelta(seconds=60)
RENEW_EVERY_S = 15.0
DEFAULT_STEP_LIMIT = 30  # plan D3 steps budget (LangGraph supersteps); T5 adds the others

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
            await s.commit()


@dataclass
class RunOutcome:
    status: str  # final status, or "skipped"
    reasons: list[str] = field(default_factory=list)
    resumed: bool = False
    skipped: str | None = None
    state: dict[str, Any] = field(default_factory=dict)


class LeaseLostError(RuntimeError):
    pass


async def run_investigation(
    *,
    org_id: uuid.UUID,
    investigation_id: uuid.UUID,
    sessionmaker: async_sessionmaker[AsyncSession],
    llm: LLMClient,
    checkpointer: BaseCheckpointSaver[Any],
    owner: str,
    hooks: Mapping[str, Hook] | None = None,
    step_limit: int = DEFAULT_STEP_LIMIT,
    renew_every_s: float = RENEW_EVERY_S,
) -> RunOutcome:
    assert_no_external_tracing()
    scope = SystemScope(org_id, "investigation")
    async with sessionmaker() as s:
        inv = await InvestigationRepository(s, scope).try_lease(investigation_id, owner, LEASE_TTL)
        await s.commit()
    if inv is None:
        return RunOutcome(status="skipped", skipped="not_leased_or_not_active")

    deps = EngineDeps(sessionmaker=sessionmaker, llm=llm, hooks=hooks or {})
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

    async def renew(main: asyncio.Task[tuple[dict[str, Any], bool]]) -> None:
        while not main.done():
            await asyncio.sleep(renew_every_s)
            async with sessionmaker() as s:
                ok = await InvestigationRepository(s, scope).renew_lease(
                    investigation_id, owner, LEASE_TTL
                )
                await s.commit()
            if not ok:
                logger.warning("investigation_lease_lost", investigation_id=str(investigation_id))
                main.cancel(LeaseLostError.__name__)
                return

    main = asyncio.create_task(drive())
    renewer = asyncio.create_task(renew(main))
    try:
        try:
            final, resumed = await main
            reasons = list(final.get("incomplete_reasons", []))
        except GraphRecursionError:
            final, resumed = dict((await graph.aget_state(config)).values), True
            reasons = [*final.get("incomplete_reasons", []), "budget:steps"]
        status = "incomplete" if reasons else "completed"
        async with sessionmaker() as s:
            await InvestigationRepository(s, scope).finish(investigation_id, owner, status, reasons)
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
        renewer.cancel()
