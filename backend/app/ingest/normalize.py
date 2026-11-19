"""Validate and normalize decoded OTLP requests into a `NormalizedBatch`.

Rules (each failure drops one record and increments `rejected[<reason>]`; nothing here raises for
bad records):
- the resource `service.name` must equal the key's service (`service_mismatch`); the exception is
  a deployment event (`event.name = deployment`) whose `deployment.service.name` is the key's
  service — a CI/CD system reporting a change to this service (stored in `deployments` only);
- ids have OTLP lengths and are not all-zero; timestamps are inside the accepted window;
- text fields are truncated to their column sizes; attributes are bounded and secrets redacted.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import KeyValue
from opentelemetry.proto.metrics.v1.metrics_pb2 import HistogramDataPoint, NumberDataPoint
from opentelemetry.proto.resource.v1.resource_pb2 import Resource

from app.ingest import attributes as attrs
from app.ingest.batch import (
    DeploymentRow,
    LogRow,
    MetricRow,
    NormalizedBatch,
    Signal,
    SpanRow,
)
from app.ingest.principal import IngestPrincipal
from app.models.telemetry import MetricKind

MAX_BODY_CHARS = 8192
MAX_STACKTRACE_CHARS = 16384
_ZERO_TRACE = bytes(16)
_ZERO_SPAN = bytes(8)


@dataclass(frozen=True, slots=True)
class Window:
    """Accepted record time range, in microseconds since the epoch."""

    now_us: int
    min_us: int
    max_us: int

    @classmethod
    def around(cls, now_us: int, *, max_age_s: int, max_future_s: int) -> Window:
        return cls(now_us, now_us - max_age_s * 1_000_000, now_us + max_future_s * 1_000_000)

    def check(self, ts_us: int) -> str | None:
        if ts_us < self.min_us:
            return "too_old"
        if ts_us > self.max_us:
            return "in_future"
        return None


@dataclass(frozen=True, slots=True)
class _Resource:
    service_name: str | None
    instance_id: str | None
    version: str | None


def _resource(resource: Resource) -> _Resource:
    r = attrs.to_dict(resource.attributes)
    name = r.get("service.name")
    return _Resource(
        service_name=name if isinstance(name, str) else None,
        instance_id=attrs.text(r.get("service.instance.id"), 128),
        version=attrs.text(r.get("service.version"), 64),
    )


def _new_batch(principal: IngestPrincipal, signal: Signal, window: Window) -> NormalizedBatch:
    return NormalizedBatch(
        org_id=principal.org_id,
        service_id=principal.service_id,
        key_id=principal.key_id,
        signal=signal,
        received_us=window.now_us,
    )


def count_records(signal: Signal, request: Any) -> int:
    """Records in a decoded request (for the per-key records/s limit and the per-request cap)."""
    if signal == "traces":
        return sum(len(ss.spans) for rs in request.resource_spans for ss in rs.scope_spans)
    if signal == "logs":
        return sum(len(sl.log_records) for rl in request.resource_logs for sl in rl.scope_logs)
    return sum(
        _point_count(m)
        for rm in request.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    )


# ---- traces ------------------------------------------------------------------------------------


def normalize_traces(
    request: ExportTraceServiceRequest, principal: IngestPrincipal, window: Window
) -> NormalizedBatch:
    batch = _new_batch(principal, "traces", window)
    for rs in request.resource_spans:
        res = _resource(rs.resource)
        spans = [s for ss in rs.scope_spans for s in ss.spans]
        if res.service_name != principal.service_name:
            batch.reject("service_mismatch", len(spans))
            continue
        for span in spans:
            row_or_reason = _span_row(span, res, window)
            if isinstance(row_or_reason, str):
                batch.reject(row_or_reason)
            else:
                batch.spans.append(row_or_reason)
    return batch


def _span_row(span: Any, res: _Resource, window: Window) -> SpanRow | str:
    if len(span.trace_id) != 16 or span.trace_id == _ZERO_TRACE:
        return "invalid_trace_id"
    if len(span.span_id) != 8 or span.span_id == _ZERO_SPAN:
        return "invalid_span_id"
    parent = span.parent_span_id or None
    if parent is not None and (len(parent) != 8 or parent == _ZERO_SPAN):
        return "invalid_parent_span_id"
    if not span.name:
        return "missing_name"
    if not 0 <= span.kind <= 5:
        return "invalid_kind"
    start_ns, end_ns = span.start_time_unix_nano, span.end_time_unix_nano
    if start_ns <= 0 or end_ns < start_ns:
        return "invalid_timestamp"
    start_us = start_ns // 1000
    if reason := window.check(start_us):
        return reason
    status_code = span.status.code if 0 <= span.status.code <= 2 else 0

    a = attrs.to_dict(span.attributes)
    exc_type = exc_message = None
    for event in span.events:
        if event.name == "exception":
            ea = attrs.to_dict(event.attributes)
            exc_type = attrs.text(ea.get("exception.type"), 256)
            exc_message = attrs.text(ea.get("exception.message"), 2048)
            break
    return SpanRow(
        trace_id=bytes(span.trace_id),
        span_id=bytes(span.span_id),
        parent_span_id=bytes(parent) if parent else None,
        name=span.name[:256],
        kind=span.kind,
        start_us=start_us,
        end_us=end_ns // 1000,
        duration_ms=(end_ns - start_ns) / 1_000_000,
        status_code=status_code,
        status_message=span.status.message[:1024] or None,
        http_method=attrs.text(a.get("http.request.method", a.get("http.method")), 16),
        http_route=attrs.text(a.get("http.route"), 256),
        http_status_code=attrs.integer(
            a.get("http.response.status_code", a.get("http.status_code"))
        ),
        db_system=attrs.text(a.get("db.system.name", a.get("db.system")), 64),
        server_address=attrs.text(a.get("server.address", a.get("net.peer.name")), 256),
        peer_service=attrs.text(a.get("peer.service"), 128),
        exception_type=exc_type,
        exception_message=exc_message,
        service_instance_id=res.instance_id,
        service_version=res.version,
        attributes=attrs.bounded(a),
    )


# ---- logs --------------------------------------------------------------------------------------


def normalize_logs(
    request: ExportLogsServiceRequest, principal: IngestPrincipal, window: Window
) -> NormalizedBatch:
    batch = _new_batch(principal, "logs", window)
    for rl in request.resource_logs:
        res = _resource(rl.resource)
        own = res.service_name == principal.service_name
        for record in (r for sl in rl.scope_logs for r in sl.log_records):
            a = attrs.to_dict(record.attributes)
            event_name = record.event_name or a.get("event.name")
            if event_name == "deployment":
                # A change event about a service, possibly emitted by a CI/CD system.
                if a.get("deployment.service.name") != principal.service_name:
                    batch.reject("service_mismatch")
                    continue
                row_or_reason: Any = _deployment_row(record, a, window)
                target: list[Any] = batch.deployments
            elif not own:
                batch.reject("service_mismatch")
                continue
            else:
                row_or_reason = _log_row(record, a, res, principal, window)
                target = batch.logs
            if isinstance(row_or_reason, str):
                batch.reject(row_or_reason)
            else:
                target.append(row_or_reason)
    return batch


def _record_ts_us(record: Any) -> int | None:
    ns = record.time_unix_nano or record.observed_time_unix_nano
    return ns // 1000 if ns > 0 else None


def _deployment_row(record: Any, a: dict[str, Any], window: Window) -> DeploymentRow | str:
    ts_us = _record_ts_us(record)
    if ts_us is None:
        return "invalid_timestamp"
    if reason := window.check(ts_us):
        return reason
    deployment_id = attrs.text(a.get("deployment.id"), 128)
    if not deployment_id:
        return "missing_deployment_id"
    return DeploymentRow(
        deployment_id=deployment_id,
        status=attrs.text(a.get("deployment.status"), 32) or "unknown",
        version=attrs.text(a.get("deployment.version"), 64),
        previous_version=attrs.text(a.get("deployment.previous_version"), 64),
        ts_us=ts_us,
    )


def _body_text(record: Any) -> str | None:
    if not record.HasField("body"):
        return None
    value = attrs.any_value(record.body)
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text[:MAX_BODY_CHARS]


def _log_row(
    record: Any,
    a: dict[str, Any],
    res: _Resource,
    principal: IngestPrincipal,
    window: Window,
) -> LogRow | str:
    ts_us = _record_ts_us(record)
    if ts_us is None:
        return "invalid_timestamp"
    if reason := window.check(ts_us):
        return reason
    trace_id = bytes(record.trace_id) or None
    span_id = bytes(record.span_id) or None
    if trace_id is not None and (len(trace_id) != 16 or trace_id == _ZERO_TRACE):
        return "invalid_trace_id"
    if span_id is not None and (len(span_id) != 8 or span_id == _ZERO_SPAN):
        return "invalid_span_id"
    severity = record.severity_number if 0 <= record.severity_number <= 24 else 0
    body = _body_text(record)
    observed = record.observed_time_unix_nano // 1000 or None
    digest = hashlib.sha256()
    for part in (
        str(principal.service_id),
        str(ts_us),
        str(severity),
        body or "",
        (trace_id or b"").hex(),
        (span_id or b"").hex(),
    ):
        digest.update(part.encode())
        digest.update(b"\x00")
    return LogRow(
        record_hash=digest.digest(),
        ts_us=ts_us,
        observed_us=observed,
        severity_number=severity,
        severity_text=record.severity_text[:32] or None,
        body=body,
        trace_id=trace_id,
        span_id=span_id,
        event_name=attrs.text(record.event_name or a.get("event.name"), 128),
        exception_type=attrs.text(a.get("exception.type"), 256),
        exception_message=attrs.text(a.get("exception.message"), 2048),
        exception_stacktrace=attrs.text(a.get("exception.stacktrace"), MAX_STACKTRACE_CHARS),
        service_instance_id=res.instance_id,
        service_version=res.version,
        attributes=attrs.bounded(a),
    )


# ---- metrics -----------------------------------------------------------------------------------


def _point_count(metric: Any) -> int:
    kind = metric.WhichOneof("data")
    if kind is None:
        return 1
    return len(getattr(metric, kind).data_points)


def normalize_metrics(
    request: ExportMetricsServiceRequest, principal: IngestPrincipal, window: Window
) -> NormalizedBatch:
    batch = _new_batch(principal, "metrics", window)
    for rm in request.resource_metrics:
        res = _resource(rm.resource)
        metrics = [m for sm in rm.scope_metrics for m in sm.metrics]
        if res.service_name != principal.service_name:
            batch.reject("service_mismatch", sum(_point_count(m) for m in metrics))
            continue
        for metric in metrics:
            _normalize_metric(metric, res, principal, window, batch)
    return batch


def _normalize_metric(
    metric: Any,
    res: _Resource,
    principal: IngestPrincipal,
    window: Window,
    batch: NormalizedBatch,
) -> None:
    kind_name = metric.WhichOneof("data")
    if kind_name not in {"gauge", "sum", "histogram"}:
        batch.reject("unsupported_metric_type", _point_count(metric))
        return
    if not metric.name:
        batch.reject("missing_name", _point_count(metric))
        return
    data = getattr(metric, kind_name)
    kind = {"gauge": MetricKind.GAUGE, "sum": MetricKind.SUM, "histogram": MetricKind.HISTOGRAM}[
        kind_name
    ]
    is_monotonic = bool(kind_name == "sum" and data.is_monotonic)
    temporality = int(getattr(data, "aggregation_temporality", 0))
    for point in data.data_points:
        row_or_reason = _metric_row(
            metric, point, kind, is_monotonic, temporality, res, principal, window
        )
        if isinstance(row_or_reason, str):
            batch.reject(row_or_reason)
        else:
            batch.metrics.append(row_or_reason)


def series_hash(
    service_id: str, name: str, kind: int, temporality: int, series_attributes: dict[str, Any]
) -> bytes:
    canonical = json.dumps(
        [service_id, name, kind, temporality, series_attributes],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).digest()


def _metric_row(
    metric: Any,
    point: NumberDataPoint | HistogramDataPoint,
    kind: MetricKind,
    is_monotonic: bool,
    temporality: int,
    res: _Resource,
    principal: IngestPrincipal,
    window: Window,
) -> MetricRow | str:
    if point.time_unix_nano <= 0:
        return "invalid_timestamp"
    ts_us = point.time_unix_nano // 1000
    if reason := window.check(ts_us):
        return reason
    value = count = total = None
    buckets: list[int] | None = None
    bounds: list[float] | None = None
    if isinstance(point, NumberDataPoint):
        which = point.WhichOneof("value")
        if which is None:
            return "invalid_value"
        value = float(getattr(point, which))
        if not math.isfinite(value):
            return "invalid_value"
    else:
        buckets = list(point.bucket_counts)
        bounds = list(point.explicit_bounds)
        if buckets and len(buckets) != len(bounds) + 1:
            return "invalid_value"
        if any(not math.isfinite(b) for b in bounds):
            return "invalid_value"
        count = int(point.count)
        total = point.sum if point.HasField("sum") and math.isfinite(point.sum) else None
    series_attributes = attrs.bounded(_with_instance(point.attributes, res))
    name = metric.name[:256]
    return MetricRow(
        series_hash=series_hash(
            str(principal.service_id), name, int(kind), temporality, series_attributes
        ),
        name=name,
        unit=metric.unit[:64] or None,
        kind=int(kind),
        is_monotonic=is_monotonic,
        temporality=temporality,
        series_attributes=series_attributes,
        ts_us=ts_us,
        start_us=point.start_time_unix_nano // 1000 or None,
        value=value,
        count=count,
        sum=total,
        bucket_counts=buckets,
        explicit_bounds=bounds,
    )


def _with_instance(point_attrs: Iterable[KeyValue], res: _Resource) -> dict[str, Any]:
    a = attrs.to_dict(point_attrs)
    if res.instance_id is not None:
        a["service.instance.id"] = res.instance_id
    return a
