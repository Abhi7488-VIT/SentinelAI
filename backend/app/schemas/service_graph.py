"""Service graph response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.service import HealthStatus


class GraphNode(BaseModel):
    """A registered service (`kind=service`) or an external system (`kind=external`).

    `id` is `service:<uuid>` or `external:<name>`; edges reference nodes by this id.
    """

    id: str
    kind: Literal["service", "external"]
    name: str
    service_id: uuid.UUID | None = None
    environment: str | None = None
    health_status: HealthStatus | None = None
    last_seen_at: datetime | None = None


class GraphEdge(BaseModel):
    id: uuid.UUID
    from_node: str
    to_node: str
    source: Literal["declared", "observed"]
    first_seen_at: datetime
    last_seen_at: datetime
    call_count: int = Field(
        description="Observed edges: calls counted in the 2-minute discovery window that last "
        "saw the edge (not a lifetime total). Declared edges: 0."
    )
    sample_trace_id: str | None = Field(
        default=None, description="Observed edges: hex trace id of a trace showing the call."
    )
    declared_dependency_id: uuid.UUID | None = Field(
        default=None, description="Declared edges: the declared dependency this edge comes from."
    )


class ServiceGraphOut(BaseModel):
    project_id: uuid.UUID
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool = Field(description="True if the edge list hit the server's hard limit.")
