# T8 correlation + incidents — evidence (2026-11-19, Codespace 4 cores / 15 GiB)

Live: migration 0008 applied (0007 → 0008); the detection tick now ends with correlation.
**No fault has been injected yet** — the S1 run (incident from a real fault) is T10 and has not
run. Live data had 0 detections, so 0 incidents is expected and proves nothing about grouping;
grouping is proven by the integration tests on Postgres (`tests/integration/test_correlation.py`).

## Live tick with correlation (worker log)

```
{"eval_end": "2026-11-19T16:41:15+00:00", "orgs": 1, "services": 5, "evaluated": 20, "fired": 0, "resolved": 0, "escalated": 0, "incidents_created": 0, "incidents_joined": 0, "duration_ms": 296.9, "event": "detection_tick_done", "
{"eval_end": "2026-11-19T16:41:30+00:00", "orgs": 1, "services": 5, "evaluated": 20, "fired": 0, "resolved": 0, "escalated": 0, "incidents_created": 0, "incidents_joined": 0, "duration_ms": 262.1, "event": "detection_tick_done", "
{"eval_end": "2026-11-19T16:41:45+00:00", "orgs": 1, "services": 5, "evaluated": 20, "fired": 0, "resolved": 0, "escalated": 0, "incidents_created": 0, "incidents_joined": 0, "duration_ms": 379.0, "event": "detection_tick_done", "
```

## Live API (operator account)

```
incidents 200 items: 0 []
detections 200 items: 0 []
detector-states?limit=3 200 items: 3 [('resource_saturation', 'ok'), ...]
transition unknown id: 404 incident_not_found
```

## `EXPLAIN (ANALYZE, BUFFERS)` of the incident list filters on 50,000 incidents

Seeded in the **test** database (one org, 20 services, 2 services per incident, statuses 80 %
resolved / 10 % open / 10 % investigating, 20 % critical), `ANALYZE`d, queries compiled from
`IncidentRepository.list_filtered` (limit 50), then deleted.

```
seeded: 50000 incidents; {'investigating': 5000, 'open': 5000, 'resolved': 40000}

== no filter
Limit (actual time=0.021..0.033 rows=51 loops=1)
  ->  Index Scan Backward using ix_incidents_org_id_created_at_id on incidents (actual time=0.020..0.029 rows=51 loops=1)
        Index Cond: (org_id = '3d9e87f5-cdee-40d0-aa75-5723b3e5e433'::uuid)
Execution Time: 0.059 ms

== status=open
Limit (actual time=0.033..0.052 rows=51 loops=1)
  ->  Index Scan Backward using ix_incidents_org_id_status_created_at_id on incidents (actual time=0.033..0.046 rows=51 loops=1)
        Index Cond: ((org_id = '3d9e87f5-cdee-40d0-aa75-5723b3e5e433'::uuid) AND ((status)::text = 'open'::text))
Execution Time: 0.084 ms

== severity=critical
Limit (actual time=0.024..0.070 rows=51 loops=1)
  ->  Index Scan Backward using ix_incidents_org_id_created_at_id on incidents (actual time=0.024..0.065 rows=51 loops=1)
        Index Cond: (org_id = '3d9e87f5-cdee-40d0-aa75-5723b3e5e433'::uuid)
Execution Time: 0.094 ms

== status=open + severity=critical
Limit (actual time=1.775..1.775 rows=0 loops=1)
  ->  Index Scan Backward using ix_incidents_org_id_status_created_at_id on incidents (actual time=1.774..1.774 rows=0 loops=1)
        Index Cond: ((org_id = '3d9e87f5-cdee-40d0-aa75-5723b3e5e433'::uuid) AND ((status)::text = 'open'::text))
Execution Time: 1.809 ms

== service_id
Limit (actual time=0.062..2.227 rows=51 loops=1)
        ->  Index Scan Backward using ix_incidents_org_id_created_at_id on incidents (actual time=0.025..0.198 rows=503 loops=1)
              Index Cond: (org_id = '3d9e87f5-cdee-40d0-aa75-5723b3e5e433'::uuid)
        ->  Index Only Scan using pk_incident_services on incident_services (actual time=0.004..0.004 rows=0 loops=503)
              Index Cond: ((org_id = '3d9e87f5-cdee-40d0-aa75-5723b3e5e433'::uuid) AND (incident_id = incidents.id) AND (service_id = '399edc3b-f3cb-4b74-a032-fc41a8010494'::uuid))
Execution Time: 2.364 ms

== q='latency in svc-1'
Limit (actual time=0.040..0.297 rows=51 loops=1)
  ->  Index Scan Backward using ix_incidents_org_id_created_at_id on incidents (actual time=0.039..0.290 rows=51 loops=1)
        Index Cond: (org_id = '3d9e87f5-cdee-40d0-aa75-5723b3e5e433'::uuid)
Execution Time: 0.327 ms

== q='#4242'
Limit (actual time=1.014..1.016 rows=1 loops=1)
        Sort Method: quicksort  Memory: 25kB
        ->  Bitmap Heap Scan on incidents (actual time=0.994..0.996 rows=1 loops=1)
              ->  BitmapOr (actual time=0.988..0.989 rows=0 loops=1)
                    ->  Bitmap Index Scan on ix_incidents_title_trgm (actual time=0.973..0.973 rows=0 loops=1)
                          Index Cond: ((title)::text ~~* '%#4242%'::text)
                    ->  Bitmap Index Scan on uq_incidents_org_id_number (actual time=0.015..0.015 rows=1 loops=1)
                          Index Cond: ((org_id = '3d9e87f5-cdee-40d0-aa75-5723b3e5e433'::uuid) AND (number = 4242))
Execution Time: 1.159 ms

== from/to (1 day)
Limit (actual time=0.597..0.626 rows=51 loops=1)
  ->  Index Scan Backward using ix_incidents_org_id_created_at_id on incidents (actual time=0.597..0.620 rows=51 loops=1)
        Index Cond: (org_id = '3d9e87f5-cdee-40d0-aa75-5723b3e5e433'::uuid)
Execution Time: 0.664 ms

cleanup: incidents left in test DB = 0
```
