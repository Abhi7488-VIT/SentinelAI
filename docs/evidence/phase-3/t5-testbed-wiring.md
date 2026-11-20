# Phase 3 T5 — testbed → SentinelAI wiring (live)

Measured on this Codespace (4 cores, 15 GiB, 32 GiB disk with 16 GiB free), 2026-11-19, testbed at
baseline traffic (~10 req/s, `LOADGEN_MIX=baseline`), SentinelAI compose stack, Collector
contrib 0.140.0 with `collector.sentinelai.yaml`.

## Connection
`make sentinel-connect REGISTER=1`: project `shop`, 5 services (environment `testbed`), declared
dependencies, 5 keys written to `testbed/.env` (mode 600). Re-run: same service ids,
`keys_revoked: 5`, `keys_created: 5`. Collector config validated with
`otelcol-contrib validate` (exit 0; a deliberately broken copy was rejected).

## All 5 services × 3 signals (2026-11-19 11:42:43 UTC, ~1 min after the Collector restart)
| service | spans | logs | series | points | last_seen_at |
|---|---|---|---|---|---|
| api-gateway | 513 | 5 | 22 | 48 | 11:42:36 |
| frontend | 438 | 5 | 22 | 44 | 11:42:32 |
| order-service | 440 | 24 | 25 | 86 | 11:42:39 |
| payment-service | 78 | 24 | 17 | 56 | 11:42:39 |
| user-service | 431 | 61 | 21 | 64 | 11:42:41 |

`ingest_usage`: accepted only (traces 1,900, metrics 298, logs 119); **0 rejected**.

## Deploy events
First round trip (payment-service 1.2.0 → 1.2.1 → 1.2.0) produced **0** `deployments` rows: the
route matched `attributes["event.name"]`, but the emitter's logging handler puts the event name in
the log record's `event_name` field (Loki had all 4 events). Routes now match
`deployment.service.name` + `deployment.id`; second round trip → 4 rows for payment-service
(`d06caa63e0e0` in_progress/succeeded 1.2.0→1.2.1, `b3861615fc59` in_progress/succeeded
1.2.1→1.2.0). The 4 events of the first round trip were not delivered to SentinelAI.

## Freshness lag and storage (10 min, 60 samples × 5 services; raw: t5-freshness-samples.csv)
Freshness = `now() − newest stored span end` per service, sampled every 10 s. It includes the
SDK's and Collector's batching and, for low-traffic services, the gap between spans, so it is an
upper bound on pipeline lag.

| | p50 | p95 | max |
|---|---|---|---|
| all services | 3.71 s | 6.68 s | 8.48 s |
| payment-service (fewest spans) | 4.55 s | 7.50 s | 8.48 s |

Over 607 s: telemetry bytes 8,257,536 → 42,999,808 (tables + indexes), spans 9,966 → 55,192,
logs 483 → 2,001, points 1,601 → 9,620. Extrapolated **per hour of baseline: ~196 MiB, ~268k
spans, ~9k logs, ~48k points**. Spans dominate (30 MB heap + 8.2 MB indexes at the end).

Consequence: 3-day span/log retention at this rate approaches ~14 GiB, close to this Codespace's
16 GiB free disk. The code default stays 3 days; this Codespace's `.env` sets spans/logs to 1 day
(at most two daily partitions kept), and compose now passes `TELEMETRY_RETENTION_*` to containers.
