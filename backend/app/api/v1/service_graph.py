"""Service dependency graph route (thin). Viewer role; tenant scoping via ViewerCtx."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.api.tenancy import ViewerCtx
from app.models.graph import ServiceEdge
from app.models.service import HealthStatus
from app.schemas.common import ErrorResponse
from app.schemas.service_graph import GraphEdge, GraphNode, ServiceGraphOut
from app.services.service_graph import GraphView, ServiceGraphService

router = APIRouter(prefix="/orgs/{org_id}/service-graph", tags=["service-graph"])

_errors: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (401, 403, 404, 422)
}


def _service_node(service_id: uuid.UUID) -> str:
    return f"service:{service_id}"


def _external_node(name: str) -> str:
    return f"external:{name}"


def _edge_out(edge: ServiceEdge) -> GraphEdge:
    target = (
        _service_node(edge.target_service_id)
        if edge.target_service_id is not None
        else _external_node(edge.target_external or "")
    )
    return GraphEdge(
        id=edge.id,
        from_node=_service_node(edge.source_service_id),
        to_node=target,
        source=edge.source,  # type: ignore[arg-type]  # DB CHECK limits it to the literal set
        first_seen_at=edge.first_seen_at,
        last_seen_at=edge.last_seen_at,
        call_count=edge.call_count,
        sample_trace_id=edge.sample_trace_id.hex() if edge.sample_trace_id else None,
        declared_dependency_id=edge.declared_dependency_id,
    )


def _graph_out(view: GraphView) -> ServiceGraphOut:
    nodes = [
        GraphNode(
            id=_service_node(s.id),
            kind="service",
            name=s.name,
            service_id=s.id,
            environment=s.environment,
            health_status=HealthStatus(s.health_status),
            last_seen_at=s.last_seen_at,
        )
        for s in view.services
    ]
    externals = sorted({e.target_external for e in view.edges if e.target_external is not None})
    nodes += [GraphNode(id=_external_node(n), kind="external", name=n) for n in externals]
    return ServiceGraphOut(
        project_id=view.project.id,
        nodes=nodes,
        edges=[_edge_out(e) for e in view.edges],
        truncated=view.truncated,
    )


@router.get("", response_model=ServiceGraphOut, responses=_errors)
async def get_service_graph(
    ctx: ViewerCtx,
    session: SessionDep,
    project_id: Annotated[uuid.UUID, Query()],
) -> ServiceGraphOut:
    """Services of a project with their declared and observed edges (both kept, by `source`)."""
    return _graph_out(await ServiceGraphService(session, ctx).get_graph(project_id))
