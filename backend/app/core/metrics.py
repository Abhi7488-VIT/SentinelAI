"""Prometheus metrics (default registry). Labels are low-cardinality by construction."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests handled, by method, route template and status.",
    ["method", "route", "status"],
)
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency by method and route template.",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
AUTH_EVENTS = Counter(
    "auth_events_total",
    "Authentication events by action and outcome.",
    ["action", "outcome"],
)

# ---- OTLP ingestion (labels are fixed enums: signal, outcome, reason) ----
INGEST_REQUESTS = Counter(
    "ingest_requests_total",
    "OTLP ingest requests by signal and outcome.",
    ["signal", "outcome"],
)
INGEST_RECORDS = Counter(
    "ingest_records_total",
    "OTLP records by signal, outcome (accepted/rejected) and rejection reason.",
    ["signal", "outcome", "reason"],
)
INGEST_QUEUE_PENDING = Gauge(
    "ingest_queue_pending",
    "Batches waiting in the ingest queue (sampled on each enqueue).",
)
