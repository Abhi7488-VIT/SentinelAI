"""Background tasks. Phase 1 has only a placeholder used to prove the broker round-trip."""

from __future__ import annotations

from datetime import UTC, datetime

from celery import shared_task


@shared_task(name="system.ping")
def ping() -> dict[str, str]:
    return {"status": "pong", "at": datetime.now(UTC).isoformat()}
