# Phase 3 T3 — ingest overload check (live, local)

Measured on this Codespace (4 cores, 15 GiB), 2026-11-19, commit before T3's commit, compose stack
(`ingest` container, `mem_limit: 384m`, default ingest settings: 50 req/s and 20,000 records/s
per key, queue cap 100 pending batches). **No writer was consuming the `ingest` queue** (T4 not
built yet), so the queue filled and stayed full — the worst case for backpressure.

Command: `uv run python -m scripts.ingest_overload --seconds 40 --concurrency 16 --spans 1000`
(gzip'd OTLP protobuf, 1,000 spans per request, 35,470-byte bodies, one key).

| Result | Value |
|---|---|
| Requests sent | 6,091 in 40.3 s (151.2 req/s) |
| 200 accepted | 109 |
| 429 rate limited (per-key records/s and requests/s) | 5,278 |
| 503 queue full (Retry-After: 5) | 704 |
| Client errors / timeouts | 0 |
| Batches left in the broker list (LLEN ingest) | 109 |
| Ingest container memory (docker stats, ~1/s) | start 78.71 MiB, peak 126.7 MiB, end 111.2 MiB of 384 MiB |
| Restarts / OOM-killed | 0 / false |

Notes:
- 109 accepted vs a cap of 100: the documented check-then-send race (`app/ingest/queue.py`) lets
  concurrent requests overshoot by at most the number in flight (16 senders here).
- The 109 test batches were deleted from Redis afterwards (no writer existed to consume them).
- Memory samples (one per second):

  - 78.71MiB / 384MiB 
  - 108MiB / 384MiB 
  - 109.1MiB / 384MiB 
  - 112.9MiB / 384MiB 
  - 114MiB / 384MiB 
  - 114.7MiB / 384MiB 
  - 107.2MiB / 384MiB 
  - 115.4MiB / 384MiB 
  - 116MiB / 384MiB 
  - 116.4MiB / 384MiB 
  - 126.7MiB / 384MiB 
  - 108.7MiB / 384MiB 
  - 114.6MiB / 384MiB 
  - 113.4MiB / 384MiB 
  - 117.2MiB / 384MiB 
  - 117.1MiB / 384MiB 
  - 117.5MiB / 384MiB 
  - 110.7MiB / 384MiB 
  - 113.4MiB / 384MiB 
  - 113.3MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
  - 111.2MiB / 384MiB 
