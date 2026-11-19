# T10 quiet baseline — 10 min, no fault (2026-11-19, Codespace 4 cores / 15 GiB)

- Window: **2026-11-19T16:54:52Z → 2026-11-19T17:04:56Z** (UTC), 600 s. Commit `15639e4` running.
- Preconditions checked at 16:54:33Z: `scenario status` → `"active": null`; loadgen mix
  `baseline` (seed 42); all 20 detectors `ok` (none `warming_up`); rollups since 15:52:15Z
  (> 30 min warm-up, so every latency baseline had 30 usable minutes); 0 detections and
  0 incidents existed before the window.
- Method: `detector_states` sampled every 5 s (118 samples), recording any row
  whose state is not `ok` **or** whose last condition is not `clear` (so `pending`, `neutral` and
  `no_data` would all show); detections/incidents queried by `created_at` in the window + 60 s
  (evaluation lag); headroom computed from `service_window_stats` and `metric_points`; loadgen
  counters diffed. No stack change (rebuild/restart) during the window.

## Result

| Measure | Value |
|---|---|
| Incidents created | **0** |
| Detections created (false positives) | **0** |
| Detector samples not `ok`/`clear` (pending, neutral, no_data, firing…) | **0** of 2360 |
| Detection ticks | 40 (one per 15-s bucket, none skipped), 800 rule evaluations |

## Raw output

```
window: 2026-11-19T16:54:52Z .. 2026-11-19T17:04:56Z (UTC)

== detections created in the window (+60 s for the 30 s evaluation lag)
 name | rule | severity | started_at | fired_at | explanation 
------+------+----------+------------+----------+-------------
(0 rows)

== detections total (all time)
 count 
-------
     0
(1 row)

== incidents created in the window
 number | title | severity | status | created_at 
--------+-------+----------+--------+------------
(0 rows)

== incidents total (all time)
 count 
-------
     0
(1 row)

== per service over the window's evaluation buckets: traffic and headroom to thresholds
      name       | buckets | requests | errors | min_req_60s | max_err_ratio_60s | max_p95_60s_ms | median_p95_60s_ms 
-----------------+---------+----------+--------+-------------+-------------------+----------------+-------------------
 api-gateway     |      40 |     6015 |      0 |         557 |            0.0000 |           79.2 |              71.2
 frontend        |      40 |     6016 |      0 |         557 |            0.0000 |           83.8 |              75.2
 order-service   |      40 |     2791 |      0 |         254 |            0.0000 |           79.8 |              74.3
 payment-service |      40 |      594 |      0 |          46 |            0.0000 |           69.2 |              65.3
 user-service    |      40 |     2703 |      0 |         223 |            0.0000 |            7.8 |               5.3
(5 rows)

== exception signatures seen in the window
 name | type | message | spans 
------+------+---------+-------
(0 rows)

== detector states now
      name       |        rule         | state | last_cond | last_value 
-----------------+---------------------+-------+-----------+------------
 api-gateway     | error_rate          | ok    | clear     |     0.0000
 api-gateway     | health_check        | ok    | clear     |     4.7008
 api-gateway     | latency_shift       | ok    | clear     |    70.7882
 api-gateway     | resource_saturation | ok    | clear     |     0.4270
 frontend        | error_rate          | ok    | clear     |     0.0000
 frontend        | health_check        | ok    | clear     |     9.6284
 frontend        | latency_shift       | ok    | clear     |    74.2682
 frontend        | resource_saturation | ok    | clear     |     0.6353
 order-service   | error_rate          | ok    | clear     |     0.0000
 order-service   | health_check        | ok    | clear     |     5.5930
 order-service   | latency_shift       | ok    | clear     |    71.3753
 order-service   | resource_saturation | ok    | clear     |     0.0000
 payment-service | error_rate          | ok    | clear     |     0.0000
 payment-service | health_check        | ok    | clear     |     5.5355
 payment-service | latency_shift       | ok    | clear     |    63.9995
 payment-service | resource_saturation | ok    | clear     |     0.0000
 user-service    | error_rate          | ok    | clear     |     0.0000
 user-service    | health_check        | ok    | clear     |     4.6985
 user-service    | latency_shift       | ok    | clear     |     5.2026
 user-service    | resource_saturation | ok    | clear     |     0.0000
(20 rows)


== detection ticks in the window (worker log)
ticks=40 evaluated_total=800 fired=0 incidents_created=0
duration_ms p50=230 p95=384 max=526
eval_end first/last: 2026-11-19T16:54:30+00:00 2026-11-19T17:04:15+00:00 distinct: 40
skipped ticks / errors / warnings / expired tasks in window: 0 (precise filter; an earlier
loose grep counted 20 lines, all `"expired": 0` fields of graph_discovery_done)

== latency headroom at window end: baseline = median per-minute p95 over 30 min before; fire threshold = max(3x, +100 ms)
      name       | baseline_minutes | baseline_ms | fire_ms | window_max_p95_ms | fraction_of_threshold 
-----------------+------------------+-------------+---------+-------------------+-----------------------
 api-gateway     |               30 |        75.6 |   226.7 |              79.2 |                  0.35
 frontend        |               30 |        81.2 |   243.7 |              83.8 |                  0.34
 order-service   |               30 |        77.3 |   232.0 |              79.8 |                  0.34
 payment-service |               30 |        68.0 |   204.1 |              69.2 |                  0.34
 user-service    |               30 |         6.5 |   106.5 |               7.8 |                  0.07
(5 rows)

== DB pool headroom in the window (max used/max, max pending; fire needs >= 0.9 AND pending > 0)
      name       | max_used | pool_max | max_pending 
-----------------+----------+----------+-------------
 order-service   |        1 |       10 |           0
 payment-service |        1 |        5 |           0
 user-service    |        0 |        5 |           0
(3 rows)

== instances first seen in the window (crash-loop needs >= 3 in 5 min)
0
== load generator during the window (delta of its cumulative counters)
mix: baseline -> baseline | requests: 6015 | errors: 0 | declined: 17 | dropped: 0
errors_by_action delta: {'browse': 0, 'account': 0, 'login': 0, 'product': 0, 'checkout': 0}
```

Sampler output (non-ok/non-clear rows): 0 lines, so no sample file is kept.

## Reading

- Margins at baseline: error ratio 0 everywhere (fire 5 %); worst p95 at 0.34× of the latency
  fire threshold (0.07× for user-service); DB pools at most 1/10 used with no waiters (fire 0.9
  with waiters); no new instances (crash loop needs 3 in 5 min); every service sent telemetry
  throughout (heartbeat).
- payment-service has the thinnest traffic (min 46 requests per 60 s), above the 20-request
  minimum, so its rules evaluated rather than reporting `no_data`.
- This is one 10-minute window at one traffic mix. It shows no false positive here; it is not a
  false-positive *rate*. Thresholds were not changed for or after this run.
