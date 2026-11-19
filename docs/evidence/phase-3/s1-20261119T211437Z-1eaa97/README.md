# T10 S1 fresh run with the fixed evaluator (run `20261119T211437Z-1eaa97`, seed 42) — **PASS**

Codespace 4 cores / 15 GiB, 2026-11-19. SentinelAI backend/worker/beat image built from `0a9b73b`
at 18:17:41Z (no backend or testbed service code changed since: `git diff 0a9b73b HEAD -- backend
testbed/services testbed/infra testbed/docker-compose*.yml` is empty). Evaluator: `harness.evaluate`
at `afcafd7` (opener taken from the incident's `created` event; pass rule unchanged — see
[`../s1-20261119T185934Z-335bba/README.md`](../s1-20261119T185934Z-335bba/README.md#re-score-with-the-fixed-evaluator-afcafd7)).
Repo HEAD at run start: `7e581b0`.

Files: `run-record.json` and `queries/` exported with `python -m harness.evidence` (Prometheus/Loki
verbatim; Jaeger responses as digests with the raw SHA-256 — raw files stay in the gitignored
`testbed/runs/`). `sentinelai-evaluation.json` is the evaluator's output file.

## Warm-up and preconditions (`preconditions.txt`)

- The Codespace was stopped overnight; telemetry resumed at **20:44:45Z** after a 66-min rollup gap.
  The run was started at 21:14:37Z so the fault (21:16:37Z) began after 30 full minutes of
  baseline (the latency rule's window; its minimum is 10). Rollup buckets 20:44:45–21:13:30Z:
  116, no gaps.
- At 21:14:34Z: no active scenario; all 20 detectors `ok`; 0 open detections; loadgen mix
  `baseline`, seed 42. Loadgen errors stayed at 211 (all from startup) between 21:01Z and 21:14Z.
- Pre-existing incidents #1 (run `e076ed`) and #2 (run `335bba`) left untouched; neither was
  active within the 10-min correlation window, and the evaluator counts 0 other incidents in this run.

## Harness (`harness-run.txt`, verbatim log): **VERIFIED**

Fault **21:16:37Z → 21:21:37Z**. All 9 symptoms observed (pool pending peak 46, pool saturated
10/10, 512 timeout log lines, order 5xx share peak 87.1 %, frontend 5xx peak 53.5 %, error traces
in order/gateway/frontend, payment-service < 1 % 5xx); baseline and recovery checks all ok.

## Evaluator output (verbatim, `evaluation.txt`; JSON in `sentinelai-evaluation.json`)

```
db-pool-exhaustion run 20261119T211437Z-1eaa97 (harness: verified): PASS
  [ok  ] exactly_one_incident: expected 1, got 1
  [ok  ] expected_services_affected: expected ['api-gateway', 'frontend', 'order-service'], got ['api-gateway', 'frontend', 'order-service']
  [ok  ] unaffected_services_excluded: expected none of ['payment-service', 'user-service'], got []
  [ok  ] grouping_is_structural: expected every joined detection has >= 1 reason, got {'7bf5a06c-df14-4104-a060-4f9e35c6f5ac': 4, 'a85273e8-1a4a-4610-abb2-acfeed81ef13': 4, 'cb9989fe-2669-452e-8759-39e5579157df': 4, 'fede57bf-09b3-4398-829f-ddc8f020604d': 3, '20af9c6c-57a0-4d23-9036-02ac93280231': 1, 'c6c4812d-fc0c-404f-a450-a2d19b7294fc': 3}
  [ok  ] detected_during_fault: expected first detection fired before 2026-11-19T21:21:37+00:00, got 2026-11-19T21:17:45+00:00
  [ok  ] explanations_readable: expected no problems, got {}
  [ok  ] no_false_positives: expected 0, got 0
  time_to_first_detection_s: 68.0
  time_to_incident_s: 112.737045
  incident_detected_after_fault_s: 68.0
  detections_in_incident: 7
  false_positive_detections: 0
  other_incidents_during_run: 0
  root_cause_service_has_detection: True
```

## Grouping (`grouping.txt`, read-only from Postgres)

Incident **#3**, "Latency, errors and resource saturation in api-gateway, frontend, order-service",
critical. Opener (`created` event 21): `13f3b2ff` order-service `error_rate`, started 21:16:45Z,
fired 21:17:45Z. Six joiners, each with ≥ 1 reason:

| detection | service | rule | started | fired | score | reasons |
|---|---|---|---|---|---|---|
| `fede57bf` | order-service | resource_saturation | 21:16:45 | 21:17:45 | 7 | 3 |
| `20af9c6c` | frontend | error_rate | 21:17:00 | 21:18:00 | 3 | 1 |
| `c6c4812d` | api-gateway | error_rate | 21:17:00 | 21:18:00 | 8 | 3 |
| `7bf5a06c` | frontend | latency_shift | 21:16:45 | 21:18:45 | 9 | 4 |
| `a85273e8` | order-service | latency_shift | 21:16:45 | 21:18:45 | 9 | 4 |
| `cb9989fe` | api-gateway | latency_shift | 21:16:45 | 21:18:45 | 9 | 4 |

## Reading

- 1 incident, the 3 expected services, 0 false positives, 0 other incidents; first detection
  fired 68 s after fault start, incident created 112.7 s after fault start (previous run: 71 s /
  103.3 s).
- In this run the opener also happens to sort first in the API's `(started_at, id)` order
  (`13f3b2ff` is the lowest id among the 21:16:45Z detections), so `172a834`'s evaluator would
  have exempted the same detection here. This run therefore does not by itself exercise the fix;
  the unit tests and the `335bba` re-score do.
- One run at one seed: it shows the golden path works on this run, not a detection rate.
  No threshold or pass criterion was changed for or after this run.
