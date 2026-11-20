"""Detection and detector-state response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.schemas.common import APIModel

RuleName = Literal[
    "error_rate", "latency_shift", "resource_saturation", "health_check", "deploy_change"
]


class DetectionOut(APIModel):
    id: uuid.UUID
    service_id: uuid.UUID
    rule: RuleName
    severity: Literal["warning", "critical"]
    metric: str
    observed_value: float | None
    baseline_value: float | None
    threshold: float | None
    peak_value: float | None
    last_value: float | None
    started_at: datetime
    fired_at: datetime
    resolved_at: datetime | None
    explanation: str = Field(description="Filled from the numbers in `evidence` (no model).")
    evidence: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class DetectorStateOut(APIModel):
    id: uuid.UUID
    service_id: uuid.UUID
    rule: RuleName
    state: Literal["ok", "pending", "firing", "resolving", "warming_up"]
    since: datetime
    last_cond: str | None
    last_value: float | None
    last_evaluated_at: datetime | None
    open_detection_id: uuid.UUID | None
    created_at: datetime
