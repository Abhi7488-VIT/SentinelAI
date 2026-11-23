"""Shared plumbing for the deterministic investigation tools (plan §4).

Tools are read-only, tenant-scoped, bounded in time (≤ 6 h per window), rows and statement time
(5 s). Service ids are re-checked against the tenant's registry, so a tool can never read another
org's telemetry even if a caller passes a foreign id.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.tenancy import TenantScope
from app.investigation.evidence import EvidenceDraft, EvidenceService
from app.models.investigation import Evidence
from app.repositories.investigation_queries import Window, bound_statement_time
from app.repositories.investigations import InvestigationRepository
from app.repositories.services import ServiceRepository

MAX_WINDOW = timedelta(hours=6)
MAX_SERVICES = 20


class ToolInputError(AppError):
    status_code = 422
    code = "invalid_tool_input"
    message = "Invalid investigation tool input."


@dataclass(frozen=True, slots=True)
class ToolContext:
    session: AsyncSession
    scope: TenantScope
    investigation_id: uuid.UUID
    node: str

    @classmethod
    async def open(
        cls, session: AsyncSession, scope: TenantScope, investigation_id: uuid.UUID, node: str
    ) -> ToolContext:
        if await InvestigationRepository(session, scope).get(investigation_id) is None:
            raise ToolInputError("investigation not found")
        return cls(session, scope, investigation_id, node)

    async def bound(self) -> None:
        await bound_statement_time(InvestigationRepository(self.session, self.scope))

    async def service_names(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        """Names of these services in this org; raises if any id is unknown here."""
        unique = list(dict.fromkeys(ids))
        if not unique:
            raise ToolInputError("at least one service is required")
        if len(unique) > MAX_SERVICES:
            raise ToolInputError(f"at most {MAX_SERVICES} services per tool call")
        found = await ServiceRepository(self.session, self.scope).get_many(unique)
        names = {s.id: s.name for s in found}
        if len(names) != len(unique):
            raise ToolInputError("unknown service")
        return names

    async def names_if_known(self, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        """Best-effort names (for services seen in traces/edges); unknown ids are omitted."""
        unique = list(dict.fromkeys(ids))
        if not unique:
            return {}
        found = await ServiceRepository(self.session, self.scope).get_many(unique)
        return {s.id: s.name for s in found}

    async def record(self, draft: EvidenceDraft) -> Evidence:
        return (
            await EvidenceService(self.session, self.scope).record(
                self.investigation_id, self.node, draft
            )
        ).evidence


def window(start: datetime, end: datetime) -> Window:
    if start.tzinfo is None or end.tzinfo is None:
        raise ToolInputError("time window must be timezone-aware")
    if not start < end:
        raise ToolInputError("time window must be non-empty")
    if end - start > MAX_WINDOW:
        raise ToolInputError("time window exceeds 6 hours")
    return Window(start.astimezone(UTC), end.astimezone(UTC))


def baseline_for(w: Window, baseline: Window | None) -> Window:
    """The given baseline (validated) or the same-length window right before `w`."""
    if baseline is None:
        return Window(w.start - (w.end - w.start), w.start)
    b = window(baseline.start, baseline.end)
    if b.end > w.start:
        raise ToolInputError("baseline must end before the incident window starts")
    return b


def fmt_t(t: datetime | None) -> str:
    return "-" if t is None else t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_w(w: Window) -> str:
    return f"[{fmt_t(w.start)}, {fmt_t(w.end)})"


def wparams(w: Window) -> dict[str, str]:
    return {"start": w.start.isoformat(), "end": w.end.isoformat()}
