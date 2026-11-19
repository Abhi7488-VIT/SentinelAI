# ADR 0005 — Telemetry storage: day-partitioned PostgreSQL tables for the MVP

- Status: **Accepted**
- Date: 2026-11-19
- Phase: 3 (Incident management)

## Context

Phase 3 ingests OTLP traces, logs and metrics and runs deterministic detection and correlation over
them (CLAUDE.md invariant 8: filtering, aggregation and grouping are SQL/Python). The data is
append-heavy, time-bounded and read mostly by **recent time window per service** (rollups every
15 s, detection evidence, dependency edges) or **by trace id** (correlation, incident detail).
CLAUDE.md fixes PostgreSQL as the MVP store and asks for time-partitioned tables, a retention job,
and this ADR; it names ClickHouse/Loki as the scale-up path.

Scale to support now: the testbed (5 services, ~10 req/s, ~5 spans per request). Measured
throughput and ceilings come from the Phase 3 T10 benchmark, not from this ADR.

## Decision

1. **Normalized tables in Postgres** (`app/models/telemetry.py`): `spans`, `log_records`,
   `metric_series` + `metric_points`, plus `deployments` (normalized deploy events) and
   `ingest_usage` (accepted/rejected counters). Columns are extracted for what queries filter on
   (service, kind, status, HTTP route/status, DB system, exception type/message); the rest goes to a
   **bounded** JSONB `attributes` (allow-listed keys, size-capped values — enforced at ingest, T3).
2. **RANGE partitions by UTC day** on the record timestamp for `spans`, `log_records` and
   `metric_points`. Retention drops whole partitions (cheap, no bloat, no long DELETEs). Defaults:
   spans and logs **3 days**, metric points **7 days**, ingest usage **30 days** (plain DELETE, small
   table). A partition is dropped when its whole day is older than the retention, so data lives
   between N and N+1 days.
3. **Partitions are managed at runtime**, not by migrations: `app.services.telemetry_maintenance`
   keeps `today − 1 … today + 2` present and drops expired days. It runs from the `migrate`
   container after `alembic upgrade` (so partitions exist before anything ingests), every 10 minutes
   from Celery beat, and (from T3) at ingest start-up. A transaction-scoped advisory lock makes
   concurrent runs skip. Alembic ignores the `*_pYYYYMMDD` tables.
4. **No DEFAULT partition.** An insert outside the prepared window fails ("no partition of
   relation") and the batch is retried, instead of silently filling an unbounded catch-all. Ingest
   only accepts records from `now − 2 h` to `now + 5 min` (T3), which the window always covers.
5. **Idempotency through primary keys** that include the partition column (Postgres requires it):
   spans `(org_id, trace_id, span_id, start_time)`, logs `(org_id, record_hash, ts)` where
   `record_hash` = SHA-256 of service, time, severity, body and trace/span ids, metric points
   `(org_id, series_id, ts)`. Writes use `INSERT … ON CONFLICT DO NOTHING`, so re-sent batches and
   redelivered queue messages insert nothing new (invariant 6). Consequence: two genuinely identical
   log lines with the same nanosecond timestamp collapse into one — accepted.
6. **Indexes follow the query list** in `docs/plans/phase-3.md` §3.2 and were checked with
   `EXPLAIN (ANALYZE, BUFFERS)` on 1M seeded spans (`docs/evidence/phase-3/t2-explain.txt`): partial
   `(org_id, service_id, start_time)` indexes for SERVER spans and for ERROR spans, the primary key
   prefix `(org_id, trace_id)` for trace lookups, `(org_id, service_id, ts)` and a partial
   `(org_id, trace_id)` on logs, `(org_id, series_id, ts)` on points.
7. **JIT is off on SentinelAI's connections.** On the seeded data Postgres chose JIT for the
   dependency-graph query: 733 ms with JIT, 16.7 ms without (rollup query 9.0 → 0.9 ms). Our queries
   are short and index-driven, so compilation cost dominates.
8. **Tenant safety in the schema**: every telemetry row references its service through
   `(org_id, service_id) → services(org_id, id)` and points reference their series through
   `(org_id, series_id)`, so telemetry cannot be attributed across tenants even by a bug.
9. **Cardinality and size limits at ingest** (T3): at most 2,000 series per service, bounded
   attributes and bodies, so one noisy service cannot grow storage without bound.

## Alternatives considered

- **ClickHouse** (columnar, excellent compression and scan speed for exactly this data). The
  scale-up path: when the T10 benchmark or real use shows Postgres ingest or window queries as the
  bottleneck, move `spans`/`log_records`/`metric_points` to ClickHouse behind the same repository
  interfaces. Not now: a second database to operate, no transactions with incident state, and the
  MVP's volume does not need it.
- **Read from the testbed's Prometheus/Loki/Jaeger directly.** Rejected: SentinelAI must work from
  telemetry it ingests itself (OTLP), not depend on one observability stack (CLAUDE.md), and Jaeger
  in the testbed is in-memory.
- **TimescaleDB** (hypertables, compression, continuous aggregates). Attractive, but an extension
  outside the fixed stack and not in the `postgres:17-alpine` image; plain partitioning covers the
  MVP. Revisit together with ClickHouse.
- **One unpartitioned table per signal with DELETE-based retention.** Simpler DDL, but deletes of
  millions of rows cause bloat and long vacuum; dropping a partition is instant.
- **Hourly partitions.** Finer retention, but ~24× more tables and planning cost for no MVP benefit.

## Consequences

- Creating or dropping a partition briefly locks the parent; doing it days ahead and every 10
  minutes keeps those moments rare and short.
- Queries must include a time range to prune partitions; repositories always do.
- Cumulative counters are stored as sent; rates are computed in SQL with resets detected via
  `start_ts`, so ingest stays stateless.
- Storage per hour of testbed baseline and ingest throughput are measured in T5/T10 and recorded in
  `docs/PROGRESS.md`.
