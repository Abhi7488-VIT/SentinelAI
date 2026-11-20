"""Incident API schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.common import APIModel
from app.schemas.detections import DetectionOut

IncidentStatus = Literal["open", "investigating", "mitigated", "resolved"]
Severity = Literal["warning", "critical"]


class ServiceRef(APIModel):
    id: uuid.UUID
    name: str
    environment: str


class IncidentOut(APIModel):
    id: uuid.UUID
    project_id: uuid.UUID
    number: int
    title: str = Field(description="Derived from affected services and rules; no root cause.")
    status: IncidentStatus
    severity: Severity
    started_at: datetime
    detected_at: datetime
    last_activity_at: datetime
    resolved_at: datetime | None
    detections_resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime
    services: list[ServiceRef]


class GroupingReason(BaseModel):
    type: Literal[
        "shared_trace",
        "exception_signature",
        "dependency_edge",
        "deploy_proximity",
        "same_service",
    ]
    detail: str
    refs: list[Any]


class IncidentDetectionOut(BaseModel):
    detection: DetectionOut
    reasons: list[GroupingReason] = Field(
        description="Why this detection was grouped; empty for the detection that opened it."
    )
    score: int
    linked_at: datetime


class IncidentDetailOut(IncidentOut):
    detections: list[IncidentDetectionOut]


Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]


class TransitionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: IncidentStatus
    note: Note | None = None


class IncidentEventOut(APIModel):
    id: int = Field(description="Monotonic; pass as after_id to continue.")
    incident_id: uuid.UUID
    type: str
    actor: Literal["system", "user"]
    actor_user_id: uuid.UUID | None
    payload: dict[str, Any]
    created_at: datetime
