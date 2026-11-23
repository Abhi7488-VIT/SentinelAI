"""Budgets and cancellation for one investigation run (Phase 4 plan §7, invariant 5).

`Guard` is checked **before** every node, every tool call and every LLM attempt; a call that
would exceed a budget is not made. Usage is read from durable rows, so a resumed run continues
from what was really spent:

- steps and tool calls: counters on the `investigations` row (incremented per execution, re-runs
  included);
- LLM calls, tokens and cost: summed from `llm_calls` (one row per attempt, failed ones too);
- wall-clock: since the row's `started_at` (first lease), not since this process started.

Exhaustion raises `BudgetExceededError(<budget>)` and cancellation `CancelRequestedError`; the
runner turns them into `incomplete` (reason `budget:<name>`) and `cancelled`. Neither changes a
hypothesis: those are written only inside the verification node's single transaction.
Tokens are checked as "already used ≥ budget" (the next call's size is not known in advance), so
one call can overshoot the token/cost budget by at most its own usage; the per-call input cap of
the LLM client bounds that.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.tenancy import SystemScope
from app.investigation.events import Events
from app.llm.errors import LLMBudgetExceededError
from app.models.investigation import Investigation
from app.repositories.investigations import InvestigationRepository
from app.repositories.llm_calls import LLMCallRepository

WARN_AT = 0.8
BUDGETS = ("steps", "tokens", "wall_clock", "tool_calls", "llm_calls", "cost")


class BudgetExceededError(Exception):
    def __init__(self, budget: str) -> None:
        super().__init__(f"budget exhausted: {budget}")
        self.budget = budget


class CancelRequestedError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Budgets:
    steps: int
    tokens: int
    wall_clock_s: int
    tool_calls: int
    llm_calls: int
    cost_usd: Decimal

    @classmethod
    def of(cls, inv: Investigation) -> Budgets:
        return cls(
            inv.budget_steps,
            inv.budget_tokens,
            inv.budget_wall_clock_s,
            inv.budget_tool_calls,
            inv.budget_llm_calls,
            Decimal(inv.budget_cost_usd),
        )


class Guard:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        scope: SystemScope,
        investigation_id: uuid.UUID,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        events: Events | None = None,
    ) -> None:
        self._sm = sessionmaker
        self._scope = scope
        self._id = investigation_id
        self._now = now
        self._events = events

    async def _warn(self, budget: str, used: float, limit: float) -> None:
        """One `budget_warning` per budget once usage reaches 80 %."""
        if self._events is not None and limit > 0 and used / limit >= WARN_AT:
            await self._events.emit(
                "budget_warning", once_key=f"budget:{budget}", budget=budget, used=used, limit=limit
            )

    async def _row(self, s: AsyncSession) -> Investigation:
        inv = await InvestigationRepository(s, self._scope).get(self._id)
        if inv is None:  # pragma: no cover - the runner holds the lease on this row
            raise CancelRequestedError("investigation disappeared")
        await s.refresh(inv)
        return inv

    async def stop_reason(self) -> str | None:
        """'cancel', 'budget:<name>' (state-based budgets only) or None. Used by the watcher."""
        async with self._sm() as s:
            inv = await self._row(s)
            if inv.cancel_requested_at is not None:
                return "cancel"
            if self._wall_clock_exceeded(inv):
                return "budget:wall_clock"
        return None

    def _wall_clock_exceeded(self, inv: Investigation) -> bool:
        started = inv.started_at or self._now()
        return self._now() - started >= timedelta(seconds=inv.budget_wall_clock_s)

    async def _check(self, s: AsyncSession) -> Investigation:
        inv = await self._row(s)
        if inv.cancel_requested_at is not None:
            raise CancelRequestedError()
        if self._wall_clock_exceeded(inv):
            raise BudgetExceededError("wall_clock")
        return inv

    async def enter(self, node: str) -> None:
        """Before a node executes: cancellation, wall-clock, steps; then count the step."""
        async with self._sm() as s:
            inv = await self._check(s)
            if inv.used_steps >= inv.budget_steps:
                raise BudgetExceededError("steps")
            used = await InvestigationRepository(s, self._scope).enter_step(self._id, node)
            await s.commit()
        await self._warn("steps", used, inv.budget_steps)
        elapsed = (self._now() - (inv.started_at or self._now())).total_seconds()
        await self._warn("wall_clock", round(elapsed, 1), inv.budget_wall_clock_s)

    async def before_tool(self) -> None:
        async with self._sm() as s:
            inv = await self._check(s)
            if inv.used_tool_calls >= inv.budget_tool_calls:
                raise BudgetExceededError("tool_calls")
            used = await InvestigationRepository(s, self._scope).add_tool_call(self._id)
            await s.commit()
        await self._warn("tool_calls", used, inv.budget_tool_calls)

    async def before_llm(self) -> None:
        """`CallContext.pre_call`: runs before every attempt (retries included)."""
        async with self._sm() as s:
            inv = await self._check(s)
            calls, tokens, cost = await LLMCallRepository(s, self._scope).totals(self._id)
        if calls >= inv.budget_llm_calls:
            raise LLMBudgetExceededError("llm_calls")
        if tokens >= inv.budget_tokens:
            raise LLMBudgetExceededError("tokens")
        if cost >= Decimal(inv.budget_cost_usd):
            raise LLMBudgetExceededError("cost")
        await self._warn("llm_calls", calls + 1, inv.budget_llm_calls)
        await self._warn("tokens", tokens, inv.budget_tokens)
        await self._warn("cost", float(cost), float(inv.budget_cost_usd))
