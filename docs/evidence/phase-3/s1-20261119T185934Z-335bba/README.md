# T10 S1 re-run after the eval_end fix (run `20261119T185934Z-335bba`, seed 42) — **FAIL** with evaluator `172a834`, **PASS** re-scored with `afcafd7`

Both verdicts are kept. The original FAIL (below, verbatim, `sentinelai-evaluation.json`) came from
an evaluator defect in picking the opener, not from SentinelAI; the re-score after the approved
fix is in [Re-score](#re-score-with-the-fixed-evaluator-afcafd7). No pass criterion changed.

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
  At the time of this analysis the evaluator was unchanged and the verdict stayed **FAIL**; the
  fix was then approved and applied (next section).
- `dump_incidents.py` (read-only public-API dump) failed here because it was run from the
  wrong directory; run it from `testbed/` with `PYTHONPATH=.`.

## Re-score with the fixed evaluator (`afcafd7`)

Evaluator change (approved 2026-11-19): `grouping_is_structural` now exempts only the detection
named by the incident's `created` event (read from `GET /incidents/{id}/events`); with no
`created` event nothing is exempt. The rule itself is identical: every joined detection must carry
≥ 1 reason. The check's `actual` is now a detection-id → reason-count map, and the result records
`opener_detection_id`. Unit tests added: detections starting in the same bucket with the opener
sorted mid-list (the shape of this run, using its UUID prefixes); a non-opener with 0 reasons listed
first still fails; no `created` event → no exemption. The first and third fail on `172a834`'s
evaluator. Testbed unit tests: 145 passed; ruff and `mypy --strict` clean.

Re-scored 2026-11-19 21:00:36Z against the same incident #2 via the public API
(`rescore-afcafd7.txt`, `sentinelai-evaluation-rescored-afcafd7.json`; the original
`sentinelai-evaluation.json` is unchanged):

```
db-pool-exhaustion run 20261119T185934Z-335bba (harness: verified): PASS
  [ok  ] exactly_one_incident: expected 1, got 1
  [ok  ] expected_services_affected: expected ['api-gateway', 'frontend', 'order-service'], got ['api-gateway', 'frontend', 'order-service']
  [ok  ] unaffected_services_excluded: expected none of ['payment-service', 'user-service'], got []
  [ok  ] grouping_is_structural: expected every joined detection has >= 1 reason, got {'01819c6b-76a5-4ea5-9ba2-726ee8588162': 3, '342dd119-a8c7-4c64-9d32-f85e9733d2f7': 1, '7ff504ea-4c57-4ad8-a3bf-1f9d24e19b4a': 4, 'bc9f21dd-4f9a-4fd5-ae6b-8eaaaae4aa4f': 3, 'c43ab8ac-da4a-4b86-a228-278a6d929f5d': 4, 'f55c189e-e698-4d22-a086-f684ff0a7072': 4}
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

`opener_detection_id` in the re-scored JSON: `510ef464-8d8a-4158-9047-26b62dd052b5`. All other
checks and metrics are identical to the original evaluation.
