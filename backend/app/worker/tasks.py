"""Background tasks."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from celery import shared_task

from app.core.config import get_settings
from app.services.telemetry_maintenance import run_maintenance
from app.worker.db import run_with_session


@shared_task(name="system.ping")
def ping() -> dict[str, str]:
    return {"status": "pong", "at": datetime.now(UTC).isoformat()}


@shared_task(name="telemetry.maintain_partitions")
def maintain_telemetry_partitions() -> dict[str, Any]:
    """Create upcoming daily partitions and drop expired ones (ADR 0005). Idempotent."""
    settings = get_settings()
    result = run_with_session(
        settings,
        lambda session: run_maintenance(session, settings),
        application_name="sentinelai-maintenance",
    )
    return asdict(result)
