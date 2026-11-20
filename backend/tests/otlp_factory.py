"""Builders for real OTLP protobuf requests (tests and local benchmarks)."""

from __future__ import annotations

import os
import time
from typing import Any, cast

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord, ResourceLogs, ScopeLogs
from opentelemetry.proto.metrics.v1.metrics_pb2 import (
    Gauge,
    Histogram,
    HistogramDataPoint,
    Metric,
    NumberDataPoint,
    ResourceMetrics,
    ScopeMetrics,
    Sum,
)
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span, Status


def now_ns() -> int:
    return time.time_ns()


def kv(key: str, value: Any) -> KeyValue:
    if isinstance(value, bool):
        v = AnyValue(bool_value=value)
    elif isinstance(value, int):
        v = AnyValue(int_value=value)
    elif isinstance(value, float):
        v = AnyValue(double_value=value)
    else:
        v = AnyValue(string_value=str(value))
    return KeyValue(key=key, value=v)


def resource(service: str, instance: str = "inst-1", version: str = "1.0.0") -> Resource:
    return Resource(
        attributes=[
            kv("service.name", service),
            kv("service.instance.id", instance),
            kv("service.version", version),
        ]
    )


def span(
    *,
    trace_id: bytes | None = None,
    span_id: bytes | None = None,
    parent_span_id: bytes = b"",
    name: str = "GET /orders",
    kind: int = Span.SPAN_KIND_SERVER,
    start_ns: int | None = None,
    duration_ns: int = 5_000_000,
    error: bool = False,
    attributes: dict[str, Any] | None = None,
    exception: tuple[str, str] | None = None,
) -> Span:
    start = start_ns if start_ns is not None else now_ns()
    s = Span(
        trace_id=trace_id if trace_id is not None else os.urandom(16),
        span_id=span_id if span_id is not None else os.urandom(8),
        parent_span_id=parent_span_id,
        name=name,
        kind=cast(Any, kind),
        start_time_unix_nano=start,
        end_time_unix_nano=start + duration_ns,
        attributes=[kv(k, v) for k, v in (attributes or {}).items()],
        status=Status(code=Status.STATUS_CODE_ERROR if error else Status.STATUS_CODE_UNSET),
    )
    if exception is not None:
        event = s.events.add()
        event.name = "exception"
        event.time_unix_nano = start
        event.attributes.extend(
            [kv("exception.type", exception[0]), kv("exception.message", exception[1])]
        )
    return s


def traces_request(service: str, spans: list[Span], **res: Any) -> ExportTraceServiceRequest:
    return ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(resource=resource(service, **res), scope_spans=[ScopeSpans(spans=spans)])
        ]
    )


def log_record(
    body: str = "request handled",
    *,
    ts_ns: int | None = None,
    severity: int = 9,
    trace_id: bytes = b"",
    span_id: bytes = b"",
    attributes: dict[str, Any] | None = None,
) -> LogRecord:
    t = ts_ns if ts_ns is not None else now_ns()
    return LogRecord(
        time_unix_nano=t,
        observed_time_unix_nano=t,
        severity_number=cast(Any, severity),
        severity_text="INFO",
        body=AnyValue(string_value=body),
        trace_id=trace_id,
        span_id=span_id,
        attributes=[kv(k, v) for k, v in (attributes or {}).items()],
    )


def logs_request(service: str, records: list[LogRecord], **res: Any) -> ExportLogsServiceRequest:
    return ExportLogsServiceRequest(
        resource_logs=[
            ResourceLogs(
                resource=resource(service, **res), scope_logs=[ScopeLogs(log_records=records)]
            )
        ]
    )


def gauge(
    name: str, value: float, *, ts_ns: int | None = None, attrs: dict[str, Any] | None = None
) -> Metric:
    point = NumberDataPoint(
        time_unix_nano=ts_ns if ts_ns is not None else now_ns(),
        as_double=value,
        attributes=[kv(k, v) for k, v in (attrs or {}).items()],
    )
    return Metric(name=name, unit="1", gauge=Gauge(data_points=[point]))


def counter(name: str, value: int, *, start_ns: int, ts_ns: int | None = None) -> Metric:
    point = NumberDataPoint(
        start_time_unix_nano=start_ns,
        time_unix_nano=ts_ns if ts_ns is not None else now_ns(),
        as_int=value,
    )
    return Metric(
        name=name,
        unit="1",
        sum=Sum(data_points=[point], is_monotonic=True, aggregation_temporality=cast(Any, 2)),
    )


def histogram(
    name: str, *, counts: list[int], bounds: list[float], ts_ns: int | None = None
) -> Metric:
    point = HistogramDataPoint(
        time_unix_nano=ts_ns if ts_ns is not None else now_ns(),
        start_time_unix_nano=1,
        count=sum(counts),
        sum=1.5,
        bucket_counts=counts,
        explicit_bounds=bounds,
    )
    return Metric(
        name=name,
        unit="s",
        histogram=Histogram(data_points=[point], aggregation_temporality=cast(Any, 2)),
    )


def metrics_request(service: str, metrics: list[Metric], **res: Any) -> ExportMetricsServiceRequest:
    return ExportMetricsServiceRequest(
        resource_metrics=[
            ResourceMetrics(
                resource=resource(service, **res), scope_metrics=[ScopeMetrics(metrics=metrics)]
            )
        ]
    )
