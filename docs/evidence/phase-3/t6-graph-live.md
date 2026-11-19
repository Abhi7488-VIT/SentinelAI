# T6 dependency graph — live check (2026-11-19, Codespace 4 cores / 15 GiB)

Stack: SentinelAI (migration 0006 applied by `migrate` at 16:04:59 UTC) + testbed at baseline,
connected through T5's Collector routing. Commit: the T6 commit on `phase-3-incident-management`.

## 1. Discovery job (beat → worker), first scheduled run

```
{"orgs": 1, "services_scanned": 5, "service_edges": 4, "external_edges": 4, "expired": 0, "skipped": {}, "duration_ms": 146.0, "event": "graph_discovery_done", "logger": "app.graph", "level": "info", "timestamp": "2026-11-19T16:06:04.155642Z"}
{"orgs": 1, "services_scanned": 5, "service_edges": 4, "external_edges": 4, "expired": 0, "skipped": {}, "duration_ms": 261.1, "event": "graph_discovery_done", "logger": "app.graph", "level": "info", "timestamp": "2026-11-19T16:07:04.230628Z"}
```

## 2. `GET /api/v1/orgs/{org}/service-graph?project_id=<shop>` (operator account, live API)

```
status 200 truncated False
nodes: [('external', 'postgresql:orders'), ('external', 'postgresql:payments'), ('external', 'postgresql:users'), ('external', 'redis:redis'), ('external', 'redis:sessions'), ('service', 'api-gateway'), ('service', 'frontend'), ('service', 'order-service'), ('service', 'payment-service'), ('service', 'user-service')]
     api-gateway -> order-service        declared  calls=0 sample=None
     api-gateway -> order-service        observed  calls=519 sample=4ff3f5cb07394018c4c9393653a7442a
     api-gateway -> user-service         declared  calls=0 sample=None
     api-gateway -> user-service         observed  calls=480 sample=b494cca084457ad9b61b1648a0020cd9
        frontend -> api-gateway          declared  calls=0 sample=None
        frontend -> api-gateway          observed  calls=1146 sample=4ff3f5cb07394018c4c9393653a7442a
   order-service -> payment-service      declared  calls=0 sample=None
   order-service -> payment-service      observed  calls=108 sample=b494cca084457ad9b61b1648a0020cd9
   order-service -> postgresql:orders    declared  calls=0 sample=None
   order-service -> postgresql:orders    observed  calls=1572 sample=4ff3f5cb07394018c4c9393653a7442a
 payment-service -> postgresql:payments  declared  calls=0 sample=None
 payment-service -> postgresql:payments  observed  calls=327 sample=5b977ada0d094d7c73f6f6b4fe009a3a
    user-service -> postgresql:users     declared  calls=0 sample=None
    user-service -> postgresql:users     observed  calls=438 sample=cc210b4a63ae17721be26054337b4e4f
    user-service -> redis:redis          observed  calls=327 sample=b494cca084457ad9b61b1648a0020cd9
    user-service -> redis:sessions       declared  calls=0 sample=None
```

Expected (plan T6 acceptance): frontend → api-gateway → {user-service, order-service},
order-service → payment-service, + Postgres/Redis externals. **Observed graph matches**; every
declared edge also has an observed twin, except `redis:sessions` (see limitations).

## 3. A sample trace is real evidence

Trace `b494cca084457ad9b61b1648a0020cd9` (cited by api-gateway → user-service and
order-service → payment-service) — server/client spans, HTTP only:

```
 frontend        | 2 | POST /checkout           | dd2ec8e9dfb359f7 |
 frontend        | 3 | POST                     | ada4abd526d31eea | dd2ec8e9dfb359f7
 api-gateway     | 2 | POST /api/orders         | c05e018433064150 | ada4abd526d31eea
 api-gateway     | 3 | GET                      | 21e1de0221a53ff1 | c05e018433064150
 user-service    | 2 | GET /sessions/{token}    | 5f20c817e311ac2b | 21e1de0221a53ff1
 api-gateway     | 3 | POST                     | 1bd4a4bfd2a161d5 | c05e018433064150
 order-service   | 2 | POST /orders             | 1d869ed2a3a03b6c | 1bd4a4bfd2a161d5
 order-service   | 3 | POST                     | 17d60958709e5ee5 | 1d869ed2a3a03b6c
 payment-service | 2 | POST /payments/authorize | 5333d150fd22930a | 17d60958709e5ee5
```

Note order-service calls `payments-lb` (toxiproxy); the edge still resolves to payment-service
because it comes from parent/child spans, not from the address.

## 4. Query plans on live data (`EXPLAIN (ANALYZE, BUFFERS)`, 2-min window)

Index names on the day partition: `…_idx1` = `(org_id, service_id, start_time) WHERE kind = 2`,
`…_idx2` = `… WHERE kind = 3 AND db_system IS NOT NULL` (new in 0006), `pkey` = dedup key.

```
== order-service: callers_of ==
                          ->  Index Scan Backward using spans_p20261119_org_id_service_id_start_time_idx1 on spans_p20261119 c (actual time=0.034..0.377 rows=527 loops=1)
                                Index Cond: ((org_id = '3d932692-4301-4540-89b4-73aa25324d3d'::uuid) AND (service_id = 'e23db41e-1864-4992-8ee0-25947d02641f'::uuid) AND (start_time >= '2026-11-19 16:03:25.888408+00'::timestamp with time zone) AND (start_time < '2026-11-19 16:05:25.888408+00'::timestamp with time zone))
                          ->  Index Scan using spans_p20261119_pkey on spans_p20261119 p (actual time=0.008..0.008 rows=1 loops=527)
                                Index Cond: ((org_id = '3d932692-4301-4540-89b4-73aa25324d3d'::uuid) AND (trace_id = c.trace_id) AND (span_id = c.parent_span_id) AND (start_time >= '2026-11-19 15:53:25.888408+00'::timestamp with time zone) AND (start_time < '2026-11-19 16:05:25.888408+00'::timestamp with time zone))
Execution Time: 5.182 ms
== order-service: database_targets ==
                    ->  Index Scan Backward using spans_p20261119_org_id_service_id_start_time_idx2 on spans_p20261119 spans (actual time=0.034..0.798 rows=1593 loops=1)
                          Index Cond: ((org_id = '3d932692-4301-4540-89b4-73aa25324d3d'::uuid) AND (service_id = 'e23db41e-1864-4992-8ee0-25947d02641f'::uuid) AND (start_time >= '2026-11-19 16:03:25.888408+00'::timestamp with time zone) AND (start_time < '2026-11-19 16:05:25.888408+00'::timestamp with time zone))
Execution Time: 1.885 ms
== api-gateway: callers_of ==
                          ->  Index Scan Backward using spans_p20261119_org_id_service_id_start_time_idx1 on spans_p20261119 c (actual time=0.030..0.472 rows=1142 loops=1)
                                Index Cond: ((org_id = '3d932692-4301-4540-89b4-73aa25324d3d'::uuid) AND (service_id = 'f4ff5bb9-442a-4b6f-8b55-4eddbc21e38e'::uuid) AND (start_time >= '2026-11-19 16:03:25.888408+00'::timestamp with time zone) AND (start_time < '2026-11-19 16:05:25.888408+00'::timestamp with time zone))
                          ->  Index Scan using spans_p20261119_pkey on spans_p20261119 p (actual time=0.004..0.005 rows=1 loops=1142)
                                Index Cond: ((org_id = '3d932692-4301-4540-89b4-73aa25324d3d'::uuid) AND (trace_id = c.trace_id) AND (span_id = c.parent_span_id) AND (start_time >= '2026-11-19 15:53:25.888408+00'::timestamp with time zone) AND (start_time < '2026-11-19 16:05:25.888408+00'::timestamp with time zone))
Execution Time: 6.892 ms
```

## Limitations found

- Redis client spans carry no `db.namespace`, so the observed external is named from
  `server.address` (`redis:redis`) and does not line up with the declared `redis:sessions`. They
  are shown as two nodes; SentinelAI does not guess that they are the same system.
- `call_count` is "calls in the 2-minute window that last saw the edge", not a lifetime total.
- Only SERVER-kind children are joined (HTTP/RPC). Messaging (PRODUCER/CONSUMER) edges and HTTP
  calls to uninstrumented hosts are not discovered yet (none exist in the testbed).
