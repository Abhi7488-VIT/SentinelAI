"""OpenTelemetry setup shared by every shop service: traces, metrics and logs over OTLP/HTTP.

One `Telemetry` per process. Exporters are configurable so tests can use in-memory ones; in the
containers they export to the Collector at OTEL_EXPORTER_OTLP_ENDPOINT. When no endpoint and no
test exporters are given, telemetry stays a no-op.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Iterable
from pathlib import Path

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, LogRecordExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

from shop.common.settings import ServiceSettings

# Stable HTTP and database semantic conventions (http.server.request.duration, db.query.text, ...)
os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http,database")

EXCLUDED_URLS = "healthz,ops/.*"
METRIC_EXPORT_INTERVAL_MS = 10_000

_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
_CGROUP = Path("/sys/fs/cgroup")
# (cgroup v2 file, cgroup v1 file)
_MEM_USAGE = (_CGROUP / "memory.current", _CGROUP / "memory/memory.usage_in_bytes")
_MEM_LIMIT = (_CGROUP / "memory.max", _CGROUP / "memory/memory.limit_in_bytes")


def _read_int(path: Path) -> int | None:
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


def _first_int(paths: tuple[Path, ...]) -> int | None:
    for path in paths:
        value = _read_int(path)
        if value is not None:
            return value
    return None


def process_rss_bytes() -> int | None:
    try:
        fields = Path("/proc/self/statm").read_text().split()
    except OSError:
        return None
    return int(fields[1]) * _PAGE_SIZE


class Telemetry:
    def __init__(
        self,
        settings: ServiceSettings,
        *,
        span_exporter: SpanExporter | None = None,
        metric_reader: MetricReader | None = None,
        log_exporter: LogRecordExporter | None = None,
    ) -> None:
        endpoint = settings.otel_exporter_otlp_endpoint
        self.enabled = endpoint is not None or span_exporter is not None
        self.instance_id = uuid.uuid4().hex
        self.resource = Resource.create(
            {
                "service.name": settings.service_name,
                "service.namespace": "shop",
                "service.version": settings.service_version,
                "service.instance.id": self.instance_id,
                "deployment.environment.name": settings.environment,
            }
        )
        if not self.enabled:
            self.tracer_provider: TracerProvider | None = None
            self.meter_provider: MeterProvider | None = None
            self.logger_provider: LoggerProvider | None = None
            return

        base = (endpoint or "").rstrip("/")
        self.tracer_provider = TracerProvider(resource=self.resource)
        self.tracer_provider.add_span_processor(
            BatchSpanProcessor(span_exporter or OTLPSpanExporter(endpoint=f"{base}/v1/traces"))
        )
        reader = metric_reader or PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"{base}/v1/metrics"),
            export_interval_millis=METRIC_EXPORT_INTERVAL_MS,
        )
        self.meter_provider = MeterProvider(resource=self.resource, metric_readers=[reader])
        self.logger_provider = LoggerProvider(resource=self.resource)
        self.logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(log_exporter or OTLPLogExporter(endpoint=f"{base}/v1/logs"))
        )
        # Globals let library instrumentations and business code find the providers.
        trace.set_tracer_provider(self.tracer_provider)
        metrics.set_meter_provider(self.meter_provider)
        self._register_process_metrics()

    # ---- logs ----------------------------------------------------------------------------

    def log_handler(self) -> logging.Handler | None:
        if self.logger_provider is None:
            return None
        return LoggingHandler(level=logging.INFO, logger_provider=self.logger_provider)

    # ---- metrics -------------------------------------------------------------------------

    def meter(self, name: str = "shop") -> metrics.Meter:
        if self.meter_provider is None:
            return metrics.NoOpMeter(name)
        return self.meter_provider.get_meter(name)

    def _register_process_metrics(self) -> None:
        meter = self.meter("shop.runtime")

        def rss(_: CallbackOptions) -> Iterable[Observation]:
            value = process_rss_bytes()
            return [Observation(value)] if value is not None else []

        def container_usage(_: CallbackOptions) -> Iterable[Observation]:
            value = _first_int(_MEM_USAGE)
            return [Observation(value)] if value is not None else []

        def container_limit(_: CallbackOptions) -> Iterable[Observation]:
            value = _first_int(_MEM_LIMIT)
            return [Observation(value)] if value is not None else []

        def cpu(_: CallbackOptions) -> Iterable[Observation]:
            return [Observation(time.process_time())]

        meter.create_observable_gauge(
            "process.memory.usage", [rss], unit="By", description="Resident set size."
        )
        meter.create_observable_gauge(
            "container.memory.usage",
            [container_usage],
            unit="By",
            description="Memory charged to the container's cgroup.",
        )
        meter.create_observable_gauge(
            "container.memory.limit",
            [container_limit],
            unit="By",
            description="The container's cgroup memory limit.",
        )
        meter.create_observable_counter(
            "process.cpu.time", [cpu], unit="s", description="CPU time used by the process."
        )

    def shutdown(self) -> None:
        for provider in (self.tracer_provider, self.meter_provider, self.logger_provider):
            if provider is not None:
                provider.shutdown()
