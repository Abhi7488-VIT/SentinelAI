"""Overview and ingest-usage response schemas (read-only aggregates from stored data)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.service import HealthStatus
from app.schemas.detections import RuleName
from app.schemas.incidents import IncidentStatus, Severity


class SeverityCounts(BaseModel):
    critical: int
    warning: int


class OverviewIncident(BaseModel):
    id: uuid.UUID
    number: int
    title: str
    status: IncidentStatus
    severity: Severity
    started_at: datetime


class RuleCount(BaseModel):
    rule: RuleName
    count: int


class MinutePoint(BaseModel):
    minute: datetime
    requests: int
    errors: int


class OverviewServiceOut(BaseModel):
    id: uuid.UUID
    name: str
    environment: str
    health_status: HealthStatus
    last_seen_at: datetime | None
    ingest_accepted_15m: int = Field(description="Records accepted in the last 15 minutes.")
    ingest_rejected_15m: int = Field(description="Records rejected in the last 15 minutes.")
    series: list[MinutePoint] = Field(
        description="Server-span requests/errors per minute, last 60 min; minutes without "
        "rollups are absent (no data), not zero."
    )


class OverviewOut(BaseModel):
    project_id: uuid.UUID
    generated_at: datetime
    open_incidents: SeverityCounts
    latest_incidents: list[OverviewIncident] = Field(description="Newest 5 by creation.")
    detections_24h: list[RuleCount] = Field(description="Detections started in the last 24 h.")
    services: list[OverviewServiceOut]
    services_truncated: bool


class SignalCounts(BaseModel):
    traces: int = 0
    logs: int = 0
    metrics: int = 0


class IngestUsageMinuteOut(BaseModel):
    minute: datetime
    accepted: SignalCounts
    rejected: SignalCounts
    rejected_by_reason: dict[str, int]
