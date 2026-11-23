"""Deterministic, read-only investigation tools (plan §4). The only creators of evidence."""

from app.investigation.tools.base import ToolContext, ToolInputError
from app.investigation.tools.dependencies import dependency_lookup
from app.investigation.tools.deploys import deploy_lookup
from app.investigation.tools.detections import detection_ref
from app.investigation.tools.logs import log_query
from app.investigation.tools.metrics import metric_query
from app.investigation.tools.traces import trace_analysis

__all__ = [
    "ToolContext",
    "ToolInputError",
    "dependency_lookup",
    "deploy_lookup",
    "detection_ref",
    "log_query",
    "metric_query",
    "trace_analysis",
]
