# T10 quiet baseline re-run after the eval_end fix — 10 min, no fault (2026-11-19)

Codespace 4 cores / 15 GiB. Re-run of [`../t10-quiet-baseline.md`](../t10-quiet-baseline.md)
(which ran commit `15639e4`) after `0a9b73b` (saturation and health rules evaluated as of
`eval_end`), to check the fix adds no false positive. Same measures; thresholds unchanged.

- Window: **2026-11-19T18:48:00Z → 2026-11-19T18:58:00Z** (UTC), 600 s.
- Running code: `backend`, `worker`, `beat` rebuilt from `0a9b73b` and recreated at 18:17:41Z
  (the worker's `app/services/detection.py` checked to contain `first_seen_between`).
  `ingest` / `ingest-writer` kept the previous image (`sha256:d363fe43…`, created 16:41Z);
  `0a9b73b` changes no ingest code. No rebuild or restart during the window
  (`raw/compose-after.txt`).
- Warm-up: the Codespace restarted at ~17:57Z, leaving one 2-min rollup gap (last bucket before
  it 17:56:30Z, first after 17:58:30Z). The window starts 30 min after the rebuild and 49.5 min
  after the gap, so every latency baseline had 30 usable minutes (query output:
  `baseline_minutes = 30` for all 5 services).
- Preconditions at 18:48:00Z (`raw/preconditions.txt`): no active scenario; all 20 detectors
  `ok`; 0 open detections; loadgen mix `baseline`, seed 42.
- State before the window: 7 detections and incident #1 exist from the failed S1 run
  `20261119T171535Z-e076ed` (all detections resolved; the incident's last activity was ~17:23Z, so
  correlation cannot pick it as a candidate: it only considers incidents active within 10 min
  before a detection's start). They were left untouched.
- Method (scripts in `tools/`, run by `tools/baseline_run.sh`; paths inside point at the session
  scratchpad where they ran): `detector_states` sampled every 5 s (116 samples), recording any row
  whose state is not `ok` **or** whose last condition is not `clear`; detections and incidents
  queried by `created_at` in the window + 60 s (evaluation lag) and detections also by
  `started_at`; headroom from `service_window_stats` and `metric_points`; detection ticks from
  the worker's JSON log; loadgen counters diffed. The earlier baseline's scripts were not
  committed; these were rewritten from its description and dry-run on 18:10–18:15Z first.

## Result

| Measure | Value |
|---|---|
| Incidents created | **0** |
| Detections created (false positives) | **0** (also 0 with `started_at` in the window) |
| Detector samples not `ok` | **0** of 2320 (116 samples × 20 detectors) |
| Detector samples `ok` but last condition not `clear` | **3** (one tick, see below) |
| Detection ticks | 40 (one per 15-s bucket, `eval_end` 18:47:30–18:57:15, none skipped), 800 rule evaluations, 0 warnings/errors |
| Tick duration | p50 236 ms, p95 386 ms, max 400 ms |
| Load generator | 6025 requests, 0 errors, 19 declined, 0 dropped |

### The 3 non-`clear` samples

```
2026-11-19 18:48:50.436048+00 | order-service | resource_saturation | ok | neutral | 0.1
2026-11-19 18:48:55.626356+00 | order-service | resource_saturation | ok | neutral | 0.1
2026-11-19 18:49:00.805409+00 | order-service | resource_saturation | ok | neutral | 0.1
```

One order-service pool reading, **18:48:13.25Z: 1/10 in use, 1 pending request**
(`raw/neutral-points.txt`; the readings 10 s before and after are 0/0). `resource_saturation`
is `clear` only with no waiters, so one waiter at 10 % utilisation is `neutral` (neither fire —
≥ 90 % **and** waiters — nor clear); from `ok`, any condition other than `fire` stays `ok`
(`app/detection/state_machine.py`), so `neutral` never starts a pending timer. The rule logic is
unchanged by `0a9b73b`.

It also shows the new clock: the reading was first used by the tick for `eval_end` 18:48:15
(window `[18:47:15, 18:48:15)`), which ran at ~18:48:45 wall clock, matching the samples at
18:48:50–18:49:00. On `15639e4` the tick for `eval_end` 18:47:45 (run at ~18:48:15, reading
`[now − 60 s, now]`) would already have used it and stamped it 28 s before the reading.

The previous baseline had 0 such samples; this one is a real, momentary waiter in the testbed's
data, not a threshold crossing. It counts as no false positive (no detection, no `pending`).

## Raw output

`raw/queries.txt` (verbatim), headline sections:

```
== per service over the window's evaluation buckets: traffic and headroom to thresholds
      name       | buckets | requests | errors | min_req_60s | max_err_ratio_60s | max_p95_60s_ms | median_p95_60s_ms
-----------------+---------+----------+--------+-------------+-------------------+----------------+-------------------
 api-gateway     |      40 |     5972 |      0 |         550 |            0.0000 |           79.2 |              70.5
 frontend        |      40 |     5972 |      0 |         550 |            0.0000 |           82.9 |              75.1
 order-service   |      40 |     2777 |      0 |         241 |            0.0000 |           77.5 |              74.8
 payment-service |      40 |      592 |      0 |          45 |            0.0000 |           67.8 |              65.6
 user-service    |      40 |     2726 |      0 |         246 |            0.0000 |            6.9 |               5.1

== latency headroom at window end
      name       | baseline_minutes | baseline_ms | fire_ms | window_max_p95_ms | fraction_of_threshold
-----------------+------------------+-------------+---------+-------------------+-----------------------
 api-gateway     |               30 |        70.6 |   211.8 |              79.2 |                  0.37
 frontend        |               30 |        74.6 |   223.7 |              82.9 |                  0.37
 order-service   |               30 |        73.0 |   219.1 |              77.5 |                  0.35
 payment-service |               30 |        65.3 |   196.0 |              67.8 |                  0.35
 user-service    |               30 |         5.0 |   105.0 |               6.9 |                  0.07

== DB pool headroom in the window (max used/max, max pending; fire needs >= 0.9 AND pending > 0)
      name       | max_used | pool_max | max_pending
-----------------+----------+----------+-------------
 order-service   |        2 |       10 |           1
 payment-service |        0 |        5 |           0
 user-service    |        0 |        5 |           0

== instances first seen in the window (crash loop needs >= 3 in 5 min)
 0
```

The latency query approximates the engine's baseline in SQL (median of `p95_60s_ms` at minute
boundaries over the 30 min before the last 60-s window, minutes with ≥ 20 requests).

## Reading

- Margins: error ratio 0 everywhere (fire 5 %); worst p95 at 0.37× of the latency fire threshold;
  DB pools at most 2/10 in use (fire 0.9 with waiters); no new instances; `health_check` was `ok`/`clear` for
  every service in all 116 samples (no heartbeat loss, no crash loop).
- Like the first baseline, this is one 10-minute window at one traffic mix: it shows no false
  positive here, not a false-positive *rate*. No threshold was changed for or after this run.
