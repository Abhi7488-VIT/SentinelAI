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
  **Not analysed yet** (usage limit reached): which detection that is, and why correlation
  linked it without a reason, still has to be established from `sentinelai-evaluation.json`
  and `GET /incidents/{id}`. No pass criterion was changed.
- `dump_incidents.py` (read-only public-API dump) failed here because it was run from the
  wrong directory; run it from `testbed/` with `PYTHONPATH=.`.
