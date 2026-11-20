# T10 S1 — DB connection-pool exhaustion vs SentinelAI (run `20261119T171535Z-e076ed`, seed 42)

Codespace 4 cores / 15 GiB, 2026-11-19. SentinelAI at commit `172a834` (detection/correlation
code from `15639e4`), evaluator `harness.evaluate` committed **before** the run (`172a834`).

## Verdict: **FAIL** (evaluator exit 1) — caused by a SentinelAI timestamp bug, not by grouping

The harness verified the fault itself (all 9 expected symptoms, payment-service < 1 % 5xx,
recovery ok): fault **17:17:35Z → 17:22:36Z**. SentinelAI produced exactly one incident with the
right services, but its first detection is stamped 20 s **before** the fault started, which
(correctly) fails the evaluator's window-based checks. Checks were not loosened.

### Evaluator output (verbatim)

```
db-pool-exhaustion run 20261119T171535Z-e076ed (harness: verified): FAIL
  [FAIL] exactly_one_incident: expected 1, got 0
  [FAIL] expected_services_affected: expected ['api-gateway', 'frontend', 'order-service'], got []
  [FAIL] unaffected_services_excluded: expected none of ['payment-service', 'user-service'], got []
  [FAIL] grouping_is_structural: expected every joined detection has >= 1 reason, got []
  [FAIL] detected_during_fault: expected first detection fired before 2026-11-19T17:22:36+00:00, got None
  [FAIL] explanations_readable: expected no problems, got {}
  [FAIL] no_false_positives: expected 0, got 1
  time_to_first_detection_s: None
  time_to_incident_s: None
  incident_detected_after_fault_s: None
  detections_in_incident: 0
  false_positive_detections: 1
  other_incidents_during_run: 1
  root_cause_service_has_detection: False
```

`exactly_one_incident: got 0` means *no incident started inside the fault window*; the one
incident (below) is listed under "other incidents" because its `started_at` is 17:17:15Z. The
other failures follow from that (no incident to inspect). The 1 false positive is the same
mis-stamped detection.

## What SentinelAI produced (public API, read-only dump)

```
#1 Resource saturation, latency and errors in order-service, api-gateway, frontend | critical open | started 2026-11-19T17:17:15Z detected 2026-11-19T17:18:15Z created 2026-11-19T17:18:47.849190Z
services: ['order-service', 'frontend', 'api-gateway']

- order-service resource_saturation (critical) started 17:17:15 fired 17:18:15 score=0
    explanation: order-service: connection pool `order-service` 10/10 in use (100.0%) with 23 pending requests at 17:18:15Z (instance efc711da86fe406f84c55edc0ef88d68), at or above 90.0% with waiters for 60 s (fires after 60 s). Top exception: `sqlalchemy.exc.TimeoutError: QueuePool limit of size # overflow # reached, connection timed out, timeout # (Background …` (301 spans). Sample traces: 6c52a92d…, 82405cc5…, 204e84e2….
    readability problems: none

- frontend latency_shift (critical) started 17:17:45 fired 17:19:45 score=9
    reason shared_trace: 5 sample trace(s) have error spans on both sides
    reason exception_signature: same exception signature
    reason dependency_edge: direct dependency edge to 1 incident service(s)
    reason same_service: the incident already has a detection on this service
    explanation: frontend: p95 latency 3107 ms over 60 s ending 17:19:45Z (604 server requests), 40.7x the baseline of 76 ms (median per-minute p95 over the previous 30 min, 30 minutes); threshold 229 ms, held 120 s (fires after 120 s). Top exception: `httpx.ReadTimeout` (5 spans). Sample traces: 36e09ab1…, f6551da1…, ee98808a….
    readability problems: none

- order-service latency_shift (critical) started 17:17:45 fired 17:19:45 score=9
    reason shared_trace: 5 sample trace(s) have error spans on both sides
    reason exception_signature: same exception signature
    reason dependency_edge: direct dependency edge to 1 incident service(s)
    reason same_service: the incident already has a detection on this service
    explanation: order-service: p95 latency 13524 ms over 60 s ending 17:19:45Z (557 server requests), 177.6x the baseline of 76 ms (median per-minute p95 over the previous 30 min, 30 minutes); threshold 228 ms, held 120 s (fires after 120 s). Top exception: `sqlalchemy.exc.TimeoutError: QueuePool limit of size # overflow # reached, connection timed out, timeout # (Background …` (421 spans). Sample traces: 8c639c5b…, 24cffb00…, d4bfe06e….
    readability problems: none

- api-gateway latency_shift (critical) started 17:17:45 fired 17:19:45 score=9
    reason shared_trace: 5 sample trace(s) have error spans on both sides
    reason exception_signature: same exception signature
    reason dependency_edge: direct dependency edge to 2 incident service(s)
    reason same_service: the incident already has a detection on this service
    explanation: api-gateway: p95 latency 3093 ms over 60 s ending 17:19:45Z (604 server requests), 42.9x the baseline of 72 ms (median per-minute p95 over the previous 30 min, 30 minutes); threshold 216 ms, held 120 s (fires after 120 s). Top exception: `shop.common.errors.UpstreamError: order-service returned #.` (231 spans). Sample traces: f6551da1…, 9748064d…, ee98808a….
    readability problems: none

- order-service error_rate (critical) started 17:17:45 fired 17:18:45 score=7
    reason shared_trace: 5 sample trace(s) have error spans on both sides
    reason exception_signature: same exception signature
    reason same_service: the incident already has a detection on this service
    explanation: order-service: error rate 80.5% (476 of 591 server requests) over 60 s ending 17:18:45Z, above the 5.0% threshold for 60 s (fires after 60 s). Baseline over the previous 30 min: 0.0%. Top exception: `sqlalchemy.exc.TimeoutError: QueuePool limit of size # overflow # reached, connection timed out, timeout # (Background …` (478 spans). Sample traces: 4968d8f8…, 2908ddd4…, df4d7f88….
    readability problems: none

- frontend error_rate (critical) started 17:18:00 fired 17:19:00 score=3
    reason shared_trace: 5 sample trace(s) have error spans on both sides
    explanation: frontend: error rate 40.1% (244 of 609 server requests) over 60 s ending 17:19:00Z, above the 5.0% threshold for 60 s (fires after 60 s). Baseline over the previous 30 min: 0.0%. Top exception: `httpx.ReadTimeout` (6 spans). Sample traces: 90850102…, 1c028d18…, 0808e98f….
    readability problems: none

- api-gateway error_rate (critical) started 17:18:00 fired 17:19:00 score=8
    reason shared_trace: 5 sample trace(s) have error spans on both sides
    reason exception_signature: same exception signature
    reason dependency_edge: direct dependency edge to 2 incident service(s)
    explanation: api-gateway: error rate 40.1% (244 of 609 server requests) over 60 s ending 17:19:00Z, above the 5.0% threshold for 60 s (fires after 60 s). Baseline over the previous 30 min: 0.0%. Top exception: `shop.common.errors.UpstreamError: order-service returned #.` (239 spans). Sample traces: 90850102…, 1c028d18…, 0808e98f….
    readability problems: none
```

Against ground truth (`expected_affected_services` = order-service, api-gateway, frontend;
`unaffected_services` = payment-service, user-service):

| Question | Answer |
|---|---|
| One incident, not several? | **Yes** — 1 incident in the whole run (#1); 7 detections, all in it. |
| Affected services | order-service, api-gateway, frontend — **exactly** the expected set; payment-service and user-service **not** included and had no detection. |
| Why grouped | Every join has structural reasons: `shared_trace` on all 6 joins (5 sample traces with error spans on both sides), plus `exception_signature` (5), `dependency_edge` (4), `same_service` (4). Scores 3–9. frontend error_rate joined on `shared_trace` only (frontend has no direct edge to order-service, which was the incident's only service at that tick). |
| Explanations readable | All 7 pass the mechanical checks (service prefix, numbers, no template leftovers, length); quoted in full above. They name the pool (10/10, 23 waiting), the `QueuePool … timed out` exception, error ratios with counts, p95 vs a 30-minute baseline. |
| Time to detect (wall clock) | Incident row created **17:18:47.8Z = 72.8 s** after fault start. First span-based detection: order-service error_rate, condition from 17:17:45Z (10 s after fault start), fired 17:18:45Z (70 s). |
| False positives | Evaluator: **1** — the mis-stamped saturation detection (see below). No detection on an unaffected service; none in the run's 2-min baseline (17:15:35–17:17:35); none after the fault. |
| Root-cause service detected | Yes (order-service: pool saturation, error rate, latency). No root cause is claimed (Phase 3). |

## The bug (evidence)

order-service pool metrics around the fault start (`metric_points`), and detection ticks with
their wall-clock completion time (worker log):

```
                 name                  |  st  |              ts               | value 
---------------------------------------+------+-------------------------------+-------
 db.client.connection.count            | used | 2026-11-19 17:17:03.946304+00 |     0
 db.client.connection.pending_requests |      | 2026-11-19 17:17:03.946304+00 |     0
 db.client.connection.count            | used | 2026-11-19 17:17:13.951875+00 |     1
 db.client.connection.pending_requests |      | 2026-11-19 17:17:13.951875+00 |     0
 db.client.connection.count            | used | 2026-11-19 17:17:23.956197+00 |     0
 db.client.connection.pending_requests |      | 2026-11-19 17:17:23.956197+00 |     0
 db.client.connection.count            | used | 2026-11-19 17:17:33.960786+00 |     0
 db.client.connection.pending_requests |      | 2026-11-19 17:17:33.960786+00 |     0
 db.client.connection.count            | used | 2026-11-19 17:17:43.964711+00 |    10
 db.client.connection.pending_requests |      | 2026-11-19 17:17:43.964711+00 |    29
 db.client.connection.count            | used | 2026-11-19 17:17:53.97782+00  |    10
 db.client.connection.pending_requests |      | 2026-11-19 17:17:53.97782+00  |    34
 db.client.connection.count            | used | 2026-11-19 17:18:04.001753+00 |    10
 db.client.connection.pending_requests |      | 2026-11-19 17:18:04.001753+00 |    32
 db.client.connection.count            | used | 2026-11-19 17:18:14.007365+00 |    10
 db.client.connection.pending_requests |      | 2026-11-19 17:18:14.007365+00 |    21
 db.client.connection.count            | used | 2026-11-19 17:18:24.014918+00 |    10
 db.client.connection.pending_requests |      | 2026-11-19 17:18:24.014918+00 |    24
(18 rows)

eval_end 17:17:00 | tick finished (wall clock) 17:17:33 | fired 0
eval_end 17:17:15 | tick finished (wall clock) 17:17:48 | fired 0
eval_end 17:17:30 | tick finished (wall clock) 17:18:03 | fired 0
eval_end 17:17:45 | tick finished (wall clock) 17:18:18 | fired 0
eval_end 17:18:00 | tick finished (wall clock) 17:18:33 | fired 0
```

- The first saturated reading is **17:17:43.96Z** (10/10 used, 29 pending) — after the fault
  started. Every earlier point is idle.
- The tick for `eval_end` 17:17:15 ran at 17:17:48 wall clock. `resource_saturation` reads metric
  points in `[now − 60 s, now]` (`app/services/detection.py`, `_condition`), so it saw the 17:17:43
  point, but the state machine stamps every rule on `eval_end` = now − 30 s. Pending began at
  17:17:15 → `started_at` 17:17:15, `fired_at` 17:18:15: both ~30 s earlier than the data.
- Span rules are not affected (they read rollups that end at `eval_end`). `health_check` has the
  same mix (silence and new instances measured at `now`, stamped at `eval_end`). No test caught
  it: the saturation integration test asserted only `fired_at − started_at = 60 s`.

## Proposed fix (not applied; awaiting go-ahead)

Evaluate every rule **as of `eval_end`** (one clock):

1. `resource_saturation`: read the latest point per series in `[eval_end − 60 s, eval_end)`
   (metrics export every 10 s; the 30 s evaluation lag covers their pipeline delay).
2. `health_check`: silence = `eval_end − last_seen_at` (0 if telemetry arrived after
   `eval_end`); new instances counted by `first_seen_at` in `[eval_end − 5 min, eval_end)` and
   `[eval_end − 2 min, eval_end)`.
3. Tests: a saturated reading at T must give `started_at` ≥ T (and pending, not firing, at
   `eval_end` < T); same for heartbeat/crash loop. These fail on the current code.
4. Re-run the quiet baseline and S1 (new run id) and evaluate with the unchanged evaluator.

Expected effect: the saturation detection would start ~17:17:45Z and fire ~17:18:45Z (≈ 30 s
later than now in data time; wall-clock time to incident roughly unchanged, since the same
data is needed). That is a prediction, to be verified by the re-run.

## Files

- `run-record.json`, `queries/` — harness run record and backend query outputs (`harness.evidence`)
- `harness-run.log` — `make scenario-run` output
- `sentinelai-evaluation.json` — evaluator result (`make evaluate-incidents RUN=…`)
- `sentinelai-incident-1.json` — the incident as returned by `GET /incidents/{id}`
