"""Investigation progress events (Phase 4 plan §8).

Payloads are built only from a per-type allow-list of fields; anything else is a programming
error. Strings are capped (an evidence summary at 1,000 chars, other values at 200), the whole
payload at 4 KiB, and no key may name a credential. Events never carry prompt text, model output
beyond a hypothesis' category/status, or raw telemetry beyond an evidence summary.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.tenancy import TenantScope
from app.models.investigation import EVENT_PAYLOAD_MAX_BYTES, EVENT_TYPES
from app.repositories.investigations import InvestigationEventRepository

ALLOWED_FIELDS: dict[str, frozenset[str]] = {
    "stage_started": frozenset({"stage"}),
    "stage_completed": frozenset({"stage"}),
    "tool_completed": frozenset(
        {"node", "tool", "evidence_id", "summary", "row_count", "truncated"}
    ),
    "llm_call": frozenset(
        {
            "node",
            "purpose",
            "provider",
            "model",
            "attempt",
            "outcome",
            "input_tokens",
            "output_tokens",
            "latency_ms",
            "cost_usd",
        }
    ),
    "hypothesis_added": frozenset(
        {"proposed_rank", "category", "subject_service_ids", "supporting_evidence_ids"}
    ),
    "hypothesis_verified": frozenset(
        {
            "rank",
            "category",
            "status",
            "status_reason",
            "checks_passed",
            "checks_applicable",
            "is_primary",
        }
    ),
    "budget_warning": frozenset({"budget", "used", "limit"}),
    "rejected_by_allowlist": frozenset({"node", "kind", "value"}),
    "status_changed": frozenset({"status", "reasons"}),
    "report_ready": frozenset({"report_id", "cited_evidence"}),
}
# Key segments that name credentials (`input_tokens` is a count, `token` would be a credential).
SECRET_SEGMENTS = frozenset(
    {
        "key",
        "apikey",
        "token",
        "password",
        "passwd",
        "secret",
        "authorization",
        "bearer",
        "credential",
        "credentials",
        "cookie",
    }
)
SUMMARY_MAX = 1000
VALUE_MAX = 200
LIST_MAX = 20


class EventPayloadError(ValueError):
    pass


def secret_like(key: str) -> bool:
    return any(seg in SECRET_SEGMENTS for seg in key.lower().replace("-", "_").split("_"))


def _clean(key: str, value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, str):
        limit = SUMMARY_MAX if key == "summary" else VALUE_MAX
        return value if len(value) <= limit else value[: limit - 1] + "…"
    if isinstance(value, list | tuple):
        return [_clean(key, v) for v in list(value)[:LIST_MAX]]
    if value is None or isinstance(value, bool | int | float):
        return value
    raise EventPayloadError(f"{key}: unsupported value type {type(value).__name__}")


def build_payload(event_type: str, fields: dict[str, Any]) -> dict[str, Any]:
    if event_type not in EVENT_TYPES:
        raise EventPayloadError(f"unknown event type {event_type!r}")
    extra = set(fields) - ALLOWED_FIELDS[event_type]
    if extra:
        raise EventPayloadError(f"{event_type}: fields not allowed: {sorted(extra)}")
    bad = [k for k in fields if secret_like(k)]
    if bad:
        raise EventPayloadError(f"{event_type}: credential-like keys: {bad}")
    payload = {k: _clean(k, v) for k, v in sorted(fields.items())}
    size = len(json.dumps(payload, ensure_ascii=False).encode())
    if size > EVENT_PAYLOAD_MAX_BYTES and isinstance(payload.get("summary"), str):
        payload["summary"] = payload["summary"][: max(0, len(payload["summary"]) - size - 64)]
    if len(json.dumps(payload, ensure_ascii=False).encode()) > EVENT_PAYLOAD_MAX_BYTES:
        raise EventPayloadError(f"{event_type}: payload over {EVENT_PAYLOAD_MAX_BYTES} bytes")
    return payload


class Events:
    """Writes events for one investigation, each in its own short transaction."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        scope: TenantScope,
        investigation_id: uuid.UUID,
    ) -> None:
        self._sm = sessionmaker
        self._scope = scope
        self._id = investigation_id

    async def emit(self, event_type: str, *, once_key: str | None = None, **fields: Any) -> None:
        async with self._sm() as s:
            await emit_in(s, self._scope, self._id, event_type, once_key=once_key, **fields)
            await s.commit()


async def emit_in(
    session: AsyncSession,
    scope: TenantScope,
    investigation_id: uuid.UUID,
    event_type: str,
    *,
    once_key: str | None = None,
    **fields: Any,
) -> None:
    """Emit inside the caller's transaction (commits with the state change it describes)."""
    await InvestigationEventRepository(session, scope).append(
        investigation_id, event_type, build_payload(event_type, fields), once_key
    )
