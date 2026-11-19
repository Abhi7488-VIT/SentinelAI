# T10 S1 re-run after the eval_end fix (run `20261119T185934Z-335bba`, seed 42) — **FAIL**

SentinelAI at `0a9b73b` (backend/worker/beat rebuilt 18:17:41Z). Harness: **VERIFIED**, fault
2026-11-19T19:01:34Z → 19:06:35Z (`harness-run.log`). Evaluator unchanged since `172a834`.

## Evaluator output (verbatim)

```
db-pool-exhaustion run 20261119T185934Z-335bba (harness: verified): FAIL
  [ok  ] exactly_one_incident: expected 1, got 1
  [ok  ] expected_services_affected: expected ['api-gateway', 'frontend', 'order-service'], got ['api-gateway', 'frontend', 'order-service']
  [ok  ] unaffected_services_excluded: expected none of ['payment-service', 'user-service'], got []
  [FAIL] grouping_is_structural: expected every joined detection has >= 1 reason, got [1, 0, 4, 3, 4, 4]
  [ok  ] detected_during_fault: expected first detection fired before 2026-11-19T19:06:35+00:00, got 2026-11-19T19:02:45+00:00
  [ok  ] explanations_readable: expected no problems, got {}
  [ok  ] no_false_positives: expected 0, got 0
  time_to_first_detection_s: 71.0
  time_to_incident_s: 103.275145
  incident_detected_after_fault_s: 71.0
  detections_in_incident: 7
  false_positive_detections: 0
  other_incidents_during_run: 0
  root_cause_service_has_detection: True
```

- The timestamp bug from run `20261119T171535Z-e076ed` is gone: 1 incident inside the fault
  window, 0 false positives, first detection fired 71 s after fault start.
- **New failure: `grouping_is_structural`.** Reason counts per joined detection were
  `[1, 0, 4, 3, 4, 4]`, so one detection joined the incident with **0** structural reasons.
  No pass criterion was changed.

## Grouping failure analysis (2026-11-19 20:50Z, read-only)

Raw output: `grouping-analysis.txt` (incident #2's events and links, queried from Postgres).

- The 0-reason detection is `510ef464` (frontend `error_rate`). It is the incident's **opener**,
  not a joiner: event 11 is `created` with `detection_id = 510ef464`, and its link has score 0 and
  `[]` reasons, which is what `_open_incident` writes (`backend/app/services/correlation.py`).
- The other 6 detections are the real joiners (events 13–18) and each has ≥ 1 reason
  (counts 1, 3, 3, 4, 4, 4; scores 3–9).
- Cause: `evaluate.py` treats `incident["detections"][0]` as the opener (`joined = members[1:]`),
  but the API orders an incident's detections by `(Detection.started_at, Detection.id)`
  (`backend/app/repositories/incidents.py:162`). All 7 detections started at 19:01:45Z, so the
  tie broke on UUID and the opener sorted third; `01819c6b` (3 reasons) was dropped from the
  check instead.
- So correlation grouped structurally; the check picked the wrong detection to exempt.
  The evaluator is **unchanged** and the recorded verdict for this run stays **FAIL** until a
  fix to how the evaluator finds the opener is approved and the run is evaluated again.
- `dump_incidents.py` (read-only public-API dump) failed here because it was run from the
  wrong directory; run it from `testbed/` with `PYTHONPATH=.`.
