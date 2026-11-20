# T0 S2 slow-sql-missing-index (run `20261120T101344Z-5c6f9c`, seed 42) — evaluator **FAIL** (incident split in two)

Codespace 4 cores / 15 GiB, 2026-11-20. SentinelAI rebuilt from `405bf12` at ~09:38Z (app code
identical to `main` `afb6fe1`); testbed services restarted 09:41Z, so the fault began after 34 min
of baseline. SentinelAI code unchanged by T0. Previous attempt `…-970f78` aborted before the fault
(see `../t0-slow-sql-missing-index-20261120T101021Z-970f78-aborted/`).

## Harness (`harness-run.txt`): **verified**

Fault 10:15:45Z → 10:20:50Z (indexes on the orders history table dropped by an allow-listed SQL
change). All symptoms observed: orders p95 peak 2.39 s, account p95 peak 2.40 s, 50 slow SELECT
traces, no error spike. Baseline and recovery checks ok.

## SentinelAI: detected, but as **two incidents**

| Incident | Title | Detections (all `latency_shift` p95, fired 10:18:15Z, started 10:16:15Z) | Grouping reasons |
|---|---|---|---|
| #4 | Latency in api-gateway, frontend | frontend (opener, p95 1141 ms vs baseline 77), api-gateway (1134 vs 72) | api-gateway: `dependency_edge` to frontend (declared + observed) |
| #5 | Latency in order-service | order-service (opener, 1191 ms vs baseline 75) | — |

No false positives (payment-service and user-service had no detection). All three expected services
had a detection 30 s after the fault started (pending) / 2.5 min (fired).

Evaluator (`evaluation.txt`, harness side, public API only): **FAIL** because
`exactly_one_incident` expected 1, got 2. Its remaining checks are computed only against a single
chosen incident, so with two incidents they print `got None` / `got []`. That is a reporting
limitation of the evaluator, not "no detection" (the detections above are in
`sentinelai-evaluation.json` → `incidents_in_fault_window`).

## Cause (read from SentinelAI's code, not changed)

`app/services/correlation.py`: each tick handles pending detections one at a time; each joins the
best-scoring active incident or opens a new one, and **no step merges two incidents** that a later
detection connects. All three detections fired in the same 15-s tick. Order: frontend opened #4;
order-service had no edge to frontend → opened #5; api-gateway joined #4 via its edge to frontend
although it also has an edge to order-service in #5. In S1 the detections fired in different
ticks, so the chain formed incrementally. This is a Phase 3 correlation gap: the result depends on
processing order within a tick.
