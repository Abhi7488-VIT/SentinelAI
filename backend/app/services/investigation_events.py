"""Server-sent events for an investigation's progress (Phase 4 plan §8).

One source of truth: the `investigation_events` table. The stream first replays every event with
`id > after` (the client's `Last-Event-ID`), then polls the table; there is no pub/sub, so replay
and live delivery cannot diverge. A heartbeat comment keeps idle connections open. The stream
ends once the investigation is terminal and every event up to its terminal `status_changed` has
been sent (both are written in the same transaction). Each poll uses its own short session.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import NotFoundError
from app.core.tenancy import TenantScope
from app.models.investigation import ACTIVE_STATUSES, InvestigationEvent
from app.repositories.investigations import InvestigationEventRepository, InvestigationRepository

BATCH = 100


class InvestigationNotFoundError(NotFoundError):
    code = "investigation_not_found"
    message = "Investigation not found."


def sse_frame(event: InvestigationEvent) -> str:
    data = json.dumps(
        {"id": event.id, "type": event.type, "at": event.created_at.isoformat(), **event.payload},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return f"id: {event.id}\nevent: {event.type}\ndata: {data}\n\n"


class InvestigationEventStream:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        scope: TenantScope,
        investigation_id: uuid.UUID,
    ) -> None:
        self._sm = sessionmaker
        self._scope = scope
        self._id = investigation_id

    async def ensure_visible(self) -> None:
        """404 for a missing or foreign investigation (checked before the stream starts)."""
        async with self._sm() as s:
            if await InvestigationRepository(s, self._scope).get(self._id) is None:
                raise InvestigationNotFoundError()

    async def frames(
        self, after: int | None, *, poll_s: float, heartbeat_s: float
    ) -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        last = after
        last_sent = loop.time()
        while True:
            async with self._sm() as s:
                inv = await InvestigationRepository(s, self._scope).get(self._id)
                # Status first, then events: a terminal status is committed together with its
                # final event, so this read sees every event up to it.
                terminal = inv is None or inv.status not in ACTIVE_STATUSES
                batch = await InvestigationEventRepository(s, self._scope).after(
                    self._id, last, BATCH
                )
            for ev in batch:
                yield sse_frame(ev)
                last = ev.id
                last_sent = loop.time()
            if len(batch) == BATCH:
                continue  # more to replay right away
            if terminal:
                return
            if loop.time() - last_sent >= heartbeat_s:
                yield ": heartbeat\n\n"
                last_sent = loop.time()
            await asyncio.sleep(poll_s)
