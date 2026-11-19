# Phase 3 T4 — ingest writer, live failure check

Measured on this Codespace, 2026-11-19 ~11:32 UTC, compose stack (`ingest`, `ingest-writer`,
`postgres`, `redis`). Script: a throwaway service `flood-service` with an ingest key, 50-span OTLP
protobuf batches A, B, C.

```
A: 200
spans after A: 50
A again: 200
spans after A twice: 50                      <- re-sent batch, no duplicate rows
postgres stopped
B (key cached, DB down): 200                 <- accepted into the bounded queue
C (key cache expired, DB down): 503 retry-after 5 ingest_unavailable
postgres started after 12.5 s
spans after recovery: 100 | s since stop: 17.3   <- B written after retries, nothing lost
```

Writer log (`docker compose logs ingest-writer`), batch B while Postgres was down:

```
{"attempt": 1, "countdown_s": 0.7, "error": "gaierror", "event": "ingest_write_retry", ... "11:32:27.086Z"}
{"attempt": 2, "countdown_s": 1.0, "error": "gaierror", "event": "ingest_write_retry", ... "11:32:27.777Z"}
{"attempt": 3, "countdown_s": 5.3, "error": "gaierror", "event": "ingest_write_retry", ... "11:32:28.833Z"}
{"attempt": 4, "countdown_s": 9.6, "error": "gaierror", "event": "ingest_write_retry", ... "11:32:34.182Z"}
```
(5th attempt succeeded; no dead letters.)

Found by planning this check and fixed before it ran: with the database down and the key not in
the 10 s cache, key lookup raised and the ingest returned **500**; it now returns **503 +
Retry-After** (unit test `test_database_down_is_503_not_500`). Run C above is that case live.
