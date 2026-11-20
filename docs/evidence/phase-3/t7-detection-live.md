# T7 detection — live check (2026-11-19, Codespace 4 cores / 15 GiB)

Migration 0007 applied by `migrate` (0006 → 0007); beat schedules `detection.tick` every 15 s.
Testbed at baseline traffic. This is **not** the T10 quiet-baseline measurement (no 30-min
warm-up window was controlled for, and it is minutes, not 10 min) — it only shows the pipeline
running on real data.

## Ticks (worker log)

```
{"eval_end": "2026-11-19T16:27:15+00:00", "orgs": 1, "services": 5, "evaluated": 20, "fired": 0, "resolved": 0, "escalated": 0, "duration_ms": 2162.2, "event": "detection_tick_done", "logger": "app.de
{"eval_end": "2026-11-19T16:27:30+00:00", "orgs": 1, "services": 5, "evaluated": 20, "fired": 0, "resolved": 0, "escalated": 0, "duration_ms": 190.6, "event": "detection_tick_done", "logger": "app.det
{"eval_end": "2026-11-19T16:27:45+00:00", "orgs": 1, "services": 5, "evaluated": 20, "fired": 0, "resolved": 0, "escalated": 0, "duration_ms": 204.6, "event": "detection_tick_done", "logger": "app.det
{"eval_end": "2026-11-19T16:28:00+00:00", "orgs": 1, "services": 5, "evaluated": 20, "fired": 0, "resolved": 0, "escalated": 0, "duration_ms": 220.9, "event": "detection_tick_done", "logger": "app.det
{"eval_end": "2026-11-19T16:28:15+00:00", "orgs": 1, "services": 5, "evaluated": 20, "fired": 0, "resolved": 0, "escalated": 0, "duration_ms": 306.0, "event": "detection_tick_done", "logger": "app.det
{"eval_end": "2026-11-19T16:28:30+00:00", "orgs": 1, "services": 5, "evaluated": 20, "fired": 0, "resolved": 0, "escalated": 0, "duration_ms": 197.2, "event": "detection_tick_done", "logger": "app.det
{"eval_end": "2026-11-19T16:28:45+00:00", "orgs": 1, "services": 5, "evaluated": 20, "fired": 0, "resolved": 0, "escalated": 0, "duration_ms": 266.3, "event": "detection_tick_done", "logger": "app.det
{"eval_end": "2026-11-19T16:29:00+00:00", "orgs": 1, "services": 5, "evaluated": 20, "fired": 0, "resolved": 0, "escalated": 0, "duration_ms": 199.4, "event": "detection_tick_done", "logger": "app.det
```

First tick = 35-min backfill for 5 services (2.16 s); steady state ~190–310 ms.

## Detector states and latest rollup row (psql)

```
      name       |        rule         | state | last_cond |   val   
-----------------+---------------------+-------+-----------+---------
 api-gateway     | error_rate          | ok    | clear     |  0.0000
 api-gateway     | health_check        | ok    | clear     |  8.5103
 api-gateway     | latency_shift       | ok    | clear     | 67.5349
 api-gateway     | resource_saturation | ok    | clear     |  0.4267
 frontend        | error_rate          | ok    | clear     |  0.0000
 frontend        | health_check        | ok    | clear     |  8.7818
 frontend        | latency_shift       | ok    | clear     | 72.4297
 frontend        | resource_saturation | ok    | clear     |  0.6353
 order-service   | error_rate          | ok    | clear     |  0.0000
 order-service   | health_check        | ok    | clear     |  5.7269
 order-service   | latency_shift       | ok    | clear     | 77.4459
 order-service   | resource_saturation | ok    | clear     |  0.0000
 payment-service | error_rate          | ok    | clear     |  0.0000
 payment-service | health_check        | ok    | clear     |  4.4286
 payment-service | latency_shift       | ok    | clear     | 66.5462
 payment-service | resource_saturation | ok    | clear     |  0.0000
 user-service    | error_rate          | ok    | clear     |  0.0000
 user-service    | health_check        | ok    | clear     |  3.6583
 user-service    | latency_shift       | ok    | clear     |  5.1875
 user-service    | resource_saturation | ok    | clear     |  0.0000
(20 rows)

 detections 
------------
          0
(1 row)

```

`resource_saturation` value on services without a DB pool is memory / declared limit.

## Query plans on live data (`EXPLAIN (ANALYZE, BUFFERS)`, api-gateway)

`…_idx1` = server-span window index `(org_id, service_id, start_time) WHERE kind = 2`;
`…_idx` = error-span window index `… WHERE status_code = 2`.

```
== rollup, 2-min recompute (8 buckets)
        ->  Nested Loop (actual time=0.052..4.589 rows=4599 loops=1)
                          ->  Index Scan using spans_p20261119_org_id_service_id_start_time_idx1 on spans_p20261119 s_1 (actual time=0.038..1.369 rows=1576 loops=1)
                                Index Cond: ((org_id = '3d932692-4301-4540-89b4-73aa25324d3d'::uuid) AND (service_id = 'f4ff5bb9-442a-4b6f-8b55-4eddbc21e38e'::uuid) AND (start_time >= ('2026-11-19 16:26:45+00'::timestamp
Execution Time: 8.210 ms
== rollup, 35-min backfill (140 buckets)
              ->  Nested Loop (actual time=0.037..265.662 rows=28163 loops=3)
                          ->  Parallel Index Scan using spans_p20261119_org_id_service_id_start_time_idx1 on spans_p20261119 s_1 (actual time=0.033..9.847 rows=7184 loops=3)
                                Index Cond: ((org_id = '3d932692-4301-4540-89b4-73aa25324d3d'::uuid) AND (service_id = 'f4ff5bb9-442a-4b6f-8b55-4eddbc21e38e'::uuid) AND (start_time >= ('2026-11-19 15:53:45+00'::timestamp
Execution Time: 330.810 ms
== exception counts (2 min)
                    ->  Index Scan using spans_p20261119_org_id_service_id_start_time_idx on spans_p20261119 spans (actual time=0.048..0.048 rows=0 loops=1)
                          Index Cond: ((org_id = '3d932692-4301-4540-89b4-73aa25324d3d'::uuid) AND (service_id = 'f4ff5bb9-442a-4b6f-8b55-4eddbc21e38e'::uuid) AND (start_time >= '2026-11-19 16:26:45+00'::timestamp with t
Execution Time: 0.125 ms
```
