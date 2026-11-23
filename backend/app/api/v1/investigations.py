"""Investigation routes (thin). T7: the progress stream (SSE); T8 adds the rest of plan §9."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import SettingsDep
from app.api.tenancy import ViewerCtx
from app.core.errors import AppError
from app.schemas.common import ErrorResponse
from app.services.investigation_events import InvestigationEventStream

router = APIRouter(prefix="/orgs/{org_id}/investigations", tags=["investigations"])

_errors: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (401, 403, 404, 422)
}


class InvalidLastEventIdError(AppError):
    status_code = 422
    code = "invalid_last_event_id"
    message = "Last-Event-ID must be a non-negative integer."


def _resume_point(header: str | None, after: int | None) -> int | None:
    if header is None or header.strip() == "":
        return after
    value = header.strip()
    if not value.isdigit() or len(value) > 19:
        raise InvalidLastEventIdError()
    return int(value)


@router.get(
    "/{investigation_id}/events",
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "Server-sent events"},
        **_errors,
    },
    response_class=StreamingResponse,
)
async def stream_events(
    ctx: ViewerCtx,
    request: Request,
    settings: SettingsDep,
    investigation_id: uuid.UUID,
    after: Annotated[int | None, Query(ge=0, description="Resume after this event id")] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """Replays every event after `Last-Event-ID` (or `after`), then streams new ones until the
    investigation is terminal. Event ids are monotonic per installation."""
    start = _resume_point(last_event_id, after)
    stream = InvestigationEventStream(request.app.state.sessionmaker, ctx, investigation_id)
    await stream.ensure_visible()
    return StreamingResponse(
        stream.frames(
            start, poll_s=settings.sse_poll_seconds, heartbeat_s=settings.sse_heartbeat_seconds
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
