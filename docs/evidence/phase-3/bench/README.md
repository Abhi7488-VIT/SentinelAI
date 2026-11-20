# Phase 3 local benchmarks (plan §12)

**Measured on this Codespace (4 cores AMD EPYC 7763, 15.6 GiB), 2026-11-20, application code at
`701056e`** (the JSON files say `dirty: true` because the two benchmark scripts were not committed
yet when they ran; no application file differed from `701056e`). Every container of SentinelAI and
the testbed kept running, so the testbed's own telemetry (~268k spans/h ≈ 75 spans/s at baseline,
T5) shared the writer and Postgres. These numbers describe this machine at that time, not a
general capacity.

## `GET /incidents` over 50,000 incidents — `incidents-list.json`

`backend/scripts/bench_incidents.py`: throwaway org via the public API, 20 services, 50,000
incidents seeded in one set-based INSERT (2 services each; 40,000 resolved / 5,000 open / 5,000
investigating; 30 % critical; one every 50 s back from now), `ANALYZE`, then per filter 20 warm-up
+ **1,000 sequential requests** over one keep-alive connection to the running backend container
(one uvicorn process), page size 50. 0 non-200 responses. Seeded rows deleted afterwards
(checked: only the 9 incidents from the testbed and e2e runs remain).

| Filter | p50 ms | p95 ms | p99 ms | items |
|---|---|---|---|---|
| none | 11.15 | 20.56 | 31.53 | 50 |
| `status=open` | 11.84 | 32.37 | 65.35 | 50 |
| `severity=critical` | 11.33 | 19.92 | 28.61 | 50 |
| `status=resolved&severity=critical` | 11.70 | 24.33 | 40.79 | 50 |
| `project_id` | 11.81 | 21.92 | 43.96 | 50 |
| `service_id` | 13.89 | 32.04 | 56.40 | 50 |
| `from`/`to` (1 day) | 11.66 | 24.71 | 44.86 | 50 |
| `q=Latency shift in svc-07` (matches 1 in 20) | 11.53 | 21.60 | 33.42 | 50 |
| `q=saturation in svc-07` (matches none) | 55.32 | 93.58 | 128.34 | 0 |
| `q=#25000` | 7.84 | 16.43 | 25.00 | 1 |

Reading: every filter's p95 is 20–32 ms. The slowest case is a title search with **no** match
(p95 93.6 ms). Not analysed with `EXPLAIN` yet; the likely cause is that every trigram candidate
must be rechecked before an empty page is known, but that is unverified. The first run of this script used that no-match query by mistake as "the" title search
(the two seeded title patterns cannot coincide); it was relabelled and a matching query added, and
the whole benchmark was re-run — the table above is the second run only (`incidents-list-output.txt`).

## OTLP ingest until rows are written — `ingest.json`

`backend/scripts/bench_ingest.py`: per level, one new service + key per sender; senders POST
protobuf (identity) batches of 1,000 records for 30 s, alternating traces and logs, all records
unique; 429/503 honoured via `Retry-After`. Rate = rows visible in Postgres / time from the first
send until the last row was visible.

| Senders | Accepted | Rows written | Until last row | Records/s | Spans/s | 200 / 503 |
|---|---|---|---|---|---|---|
| 1 | 113,000 | 113,000 | 47.7 s | 2,370 | 1,195 | 113 / 5 |
| 4 | 114,000 | 114,000 | 57.4 s | 1,987 | 994 | 114 / 20 |
| 8 | 110,000 | 110,000 | 58.0 s | 1,896 | 1,000 | 110 / 40 |

Reading: one sender already fills the bounded queue (100 pending batches), so 503 + `Retry-After:
5` throttles the senders; more senders do not raise throughput — **the writer is the bottleneck at
≈ 1.9–2.4k records/s on this machine**, ~25–32× the testbed's baseline span rate. Every accepted record was
written (no loss, no duplicates: rows = accepted). Peak during the run (`ingest-docker-stats.txt`,
49 samples every ~2 s): ingest 128.6 MiB of 384 MiB (CPU 90 %), ingest-writer 308.8 MiB of
512 MiB (CPU 189 %), Postgres 744.7 MiB (CPU 170 %).

Earlier attempts of this script (not results): run 1 waited forever because its log fixture sent
1,000 identical log records per batch, which the writer correctly stores once (dedup on content
hash + timestamp); the wait then outlived the 15-min access token. Run 2 failed in the malformed
step (parsed a protobuf OTLP response as JSON). Both were script bugs; the table is run 3.

## Malformed and partially invalid input, live — `ingest.json` → `malformed`

| Sent | Response | `ingest_records_total{outcome="rejected"}` delta | `GET /services/{id}/ingest-usage` |
|---|---|---|---|
| 66 random bytes as protobuf traces | **400** `malformed_payload` | `reason="malformed_payload"` +1 | rejected `malformed_payload: 1` |
| 10 spans, 3 with a 3-byte trace id | **200**, `partial_success.rejected_spans = 3`, `"rejected: invalid_trace_id=3"` | `reason="invalid_trace_id"` +3 | accepted traces 7, rejected `invalid_trace_id: 3` |

Overload (429/503 under flood, memory bounded) was measured live in T3 (`../t3-overload.md`) and
again here (503s above); duplicate batches live in T4 (`../t4-writer-live.md`).
