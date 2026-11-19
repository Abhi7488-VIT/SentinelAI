# Testbed

The testbed is the system SentinelAI is evaluated against: a small shop (five services) instrumented
with OpenTelemetry, a load generator, and a fault controller that injects five reproducible
incidents and records **ground truth** for each run. Phase 2 of `docs/plans/phase-2.md`;
integrity rules in ADR 0003; why live checks run in a Codespace or GitHub Actions in ADR 0004.

Everything below that is a number was measured live on 2026-11-17 in a 4-core / 16 GB Codespace.
Run ids point to records under `testbed/runs/` (gitignored) and to committed copies under
`docs/evidence/phase-2/`.

## 1. Layout and integrity

| Path | What it is | SentinelAI may read it? |
|---|---|---|
| `testbed/services/` | The monitored system: frontend → api-gateway → user-service / order-service → payment-service, Postgres, Redis | yes (Phase 5 code investigation) and its telemetry |
| `testbed/harness/` | Load generator, deploy pipeline, fault controller, symptom checks, live checks | **no** |
| `testbed/scenarios/` | Ground-truth definitions (`<id>.json`) and their JSON schema | **no** |
| `testbed/runs/` | Run records and every query output (gitignored) | **no** |

Faults act only through things that happen to real systems (a traffic pattern, a schema change, a
deploy, a refused network link). Nothing in the services or their telemetry names a fault; a unit
test greps `testbed/services/` for tell-tale words, and the controller itself is not instrumented.

## 2. Running it

```bash
make testbed-up                 # build + start, waits until every service is healthy
make testbed-status | testbed-logs | testbed-down [CLEAN=1]
make testbed-ui                 # Grafana on 127.0.0.1:3001 (off by default)
make testbed-test               # ruff, mypy --strict, unit tests (no containers)
make scenario-list | scenario-validate | scenario-status
make scenario-run ID=<id> [SEED=n] [BASELINE=s] [OBSERVE=s] [RECOVERY=s] [DRY_RUN=1]
make scenario-start ID=<id> / scenario-stop ID=<id>   # inject / remove by hand, no verdict
make deploy SERVICE=x VERSION=y [CONFIG='K=V ...']
```

`testbed/tb.sh` creates `testbed/.env` from `.env.example` with random secrets (and adds keys that
were added later). **Do not run `make testbed-up` while a scenario is running**: it recreates the
controller and kills the run without its cleanup (seen live: run `20261117T192356Z-83833b`; the
traffic mix came back as baseline because the load generator was recreated too).

Ports (127.0.0.1 only) and memory limits (each container has `memswap_limit = mem_limit`, so no
container can use swap; reaching the limit OOM-kills only that container):

| Container | Port | Limit | | Container | Port | Limit |
|---|---|---|---|---|---|---|
| frontend | 8081 | 128 MiB | | postgres | — | 384 MiB |
| api-gateway | — | 192 MiB | | redis | — | 64 MiB |
| user-service | — | 160 MiB | | toxiproxy | — | 48 MiB |
| order-service | — | 160 MiB | | otel-collector | 4318 | 192 MiB |
| payment-service | — | 160 MiB | | prometheus | 9090 | 256 MiB |
| loadgen | 8089 | 128 MiB | | loki | 3100 | 256 MiB |
| controller | 8090 | 128 MiB | | jaeger | 16686 | **384 MiB** |
| seed (one-shot) | — | 192 MiB | | grafana (profile `ui`) | 3001 | 160 MiB |

Long-running limits total 2,640 MiB (+192 MiB while the seed job runs). Jaeger was planned at
256 MiB / 10,000 traces; measured live it sat at ~235 MiB under baseline traffic alone and restarted
mid-fault in S1 at both 10,000 and 3,000 traces. At 384 MiB / 2,000 traces it peaked at 266.5 MiB
during S1 with no restart.

## 3. Telemetry

Every service exports traces, metrics and logs over OTLP to the Collector, which feeds Prometheus
(OTLP receiver), Loki (OTLP) and Jaeger v2 (in-memory). Labels used by the checks:
`service_name`, `service_instance_id`, `http_route`, `http_request_method`,
`http_response_status_code`; pool metrics `db_client_connection_*`; process and container memory
`process_memory_usage_bytes`, `container_memory_usage_bytes`, `container_memory_limit_bytes`;
deploy events in Loki under `service_name="deploy-pipeline"` with structured metadata
`deployment_service_name`, `deployment_version`, `deployment_status`.

Backend behaviour the checks depend on (all found live):
- **Jaeger 2.12's in-memory store cannot search the derived `error` tag** (`{"error":"true"}` and
  `otel.status_code=ERROR` return nothing even though the spans carry both). Trace checks match a
  real attribute instead (`http.response.status_code`, `error.type`).
- **Prometheus keeps a series' last sample for 5 minutes** after it stops (OTLP push has no
  staleness markers). After a restart, `max(gauge)` still sees the dead instance; checks that take
  a max across instances use `max_over_time(...[30s])`.
- **A refused connection records no `http.client.request.duration` sample** (httpx
  instrumentation), so connection failures are visible in client spans and logs, not that metric.
- Each service exports spans in batches: a checkout trace is only complete ~30 s after it ends.

### Sending telemetry to SentinelAI (Phase 3)

```
docker compose up -d                 # SentinelAI stack (repo root); creates network sentinelai-ingest
make sentinel-connect REGISTER=1     # register project `shop` + 5 services, write per-service keys
make testbed-up                      # Collector restarts with the SentinelAI overlay
```

- `make sentinel-connect` uses `SENTINELAI_EMAIL` / `SENTINELAI_PASSWORD` (environment or the
  repo-root `.env`), applies `testbed/sentinelai-registry.json` through SentinelAI's public API
  (services, environment `testbed`, declared dependencies, container memory limits), rotates one
  ingest key per service and writes `SENTINELAI_OTLP_ENDPOINT` + `SENTINELAI_KEY_*` to
  `testbed/.env` (gitignored, mode 600). It is idempotent. The registry holds no scenario or fault
  information (ADR 0003).
- When `SENTINELAI_OTLP_ENDPOINT` is set, `tb.sh` adds `docker-compose.sentinelai.yml` (the
  Collector joins `sentinelai-ingest`) and the Collector loads `infra/otel/collector.sentinelai.yaml`:
  `routing` connectors send each `service.name` to its own `otlphttp` exporter with that service's
  key; deploy events are routed by `deployment.service.name` + `deployment.id` (the event name may
  arrive in the record's `event_name` field, not as an attribute). Exporter queues are bounded in
  bytes (8 MiB each).
- Prometheus, Loki and Jaeger keep receiving everything as before.

## 4. Load generator

Seeded (`SEED`, default 42) arrivals and journeys for 200 of the 5,000 seeded users: browse,
product page, login, account (order history) and checkout, ~10 req/s. A mix switch restarts the
schedule at t=0 with the run's seed. Scenario mixes add one fixed-rate stream:
`with-weekly-reports` (4/s `GET /internal/reports/sales?days=7`, S1) and `cache-busting-client`
(45/s product lists with a unique `?v=<nonce>`, S5).

10-minute baseline (run `20261117T184537Z-c99f0a`): 5,958 requests, 9.93 req/s, **0 errors**,
all five services present in Prometheus, Jaeger and Loki, 5/5 settled checkout traces span all
five services. Deploy round-trip (`20261117T154229Z-719b99`): payment-service 1.2.0 → 1.2.1 in
3.1 s, version label in Prometheus, both deploy events in Loki, rollback ok.

## 5. Fault controller

One package (`testbed/harness/controller`), two front ends that call the same code: the CLI
(`make scenario-*`, runs inside the controller container) and HTTP on 127.0.0.1:8090 (every
endpoint but `/healthz` needs `Authorization: Bearer $CONTROLLER_TOKEN`; 401 otherwise, verified
live). Endpoints: `POST /scenarios/{id}/start` (202, idempotent), `POST /scenarios/{id}/stop`,
`GET /runs/{run_id}`, `GET /scenarios`.

**Run lifecycle:** reset → baseline traffic (seeded) → baseline window (default 120 s) → baseline
checks → trigger → observe window (300 s) → symptom checks → stop → recovery window (180 s) →
recovery checks (judged on its second half) → verdict + run record.

**Verdict (never assumed):** `verified` only if the baseline was healthy (otherwise the fault is
**not injected**), every expected symptom was observed in the observe window, and every recovery
check held. Anything else is `failed`, or `aborted` (memory guard / stop request). The fault is
always removed: stop runs in a `finally` once a trigger was attempted. A `dry_run` never injects.

**Checks** are a query plus a condition. PromQL/LogQL run as range queries at a 15 s step:
`min_points` = consecutive points (sustained), `min_fraction` = share of all points. Relative
thresholds use the median of the same query over the run's own baseline window, never below
`min_value`. Jaeger checks count matching traces (newest 50) in the window. Every query and raw
response is saved under `runs/<id>/<run_id>/queries/`.

**Safety:** no Docker socket. Primitives act only on allow-lists — traffic mixes, the Toxiproxy
link `order-to-payment`, the named SQL change `drop-order-history-indexes`, restarts and deploys of
the five shop services through their token-protected ops endpoints. One run at a time (lock file;
a dead holder's lock is reclaimed). The controller reads `MemAvailable` every second and aborts
(removing the fault) below 500 MiB.

**Ground truth:** `testbed/scenarios/schema/ground-truth.schema.json`. A definition carries the
injected fault, root cause (category, service, component, summary, code location), expected
symptoms, recovery checks and affected/unaffected services; a run record adds the timeline and the
run (seed, windows, steps, every observation with its evidence file, fingerprint, memory).
`python -m harness.evidence` exports a run for committing (Jaeger responses as digests).

## 6. Scenarios

Each section is generated from the definition and the verified run named in it. "baseline" is the
median over the run's own baseline window (relative checks only); onsets are seconds after the
trigger. Trace-check onsets are the earliest of the newest 50 matching traces, **not** when the
symptom began.


### 1. `db-pool-exhaustion` — DB connection-pool exhaustion in order-service

- **Trigger:** traffic mix with-weekly-reports. An internal job starts calling order-service GET /internal/reports/sales?days=7 at a fixed, seeded rate. The report keeps its database connection checked out while it streams the week's orders and calls payment-service once per chunk, so concurrent reports hold most of order-service's 10-connection pool (overflow 0, 3 s checkout timeout). Normal requests then wait for a connection and time out.
- **Root cause:** resource_exhaustion · order-service — The weekly sales report holds a pooled DB connection for its whole run, including one payment-service call per 500-order chunk; a steady stream of reports keeps the pool exhausted, so other order-service requests time out waiting for a connection (503) and the errors propagate to api-gateway and frontend.
- **Component:** database connection pool (orders DB, pool_size 10, max_overflow 0, timeout 3 s) · code location: `services/shop/order_service/main.py` · `sales_report`
- **Affected:** order-service, api-gateway, frontend · **unaffected:** payment-service, user-service
- **Stop:** traffic mix back to baseline (the report job stops)

Run `20261117T194200Z-892d6a` (seed 42, windows 120/300/180 s): **verified**. Fault 2026-11-17T19:44:00+00:00 → 2026-11-17T19:49:00+00:00.

| phase | check | result | baseline | threshold | peak | onset | points met | query |
|---|---|---|---|---|---|---|---|---|
| before | `order-pool-idle` | ok | - | 1 | 0 | 0.0 s | 9/9 | `max(avg_over_time(db_client_connection_pending_requests{service_name="order-service"}[1m]))` |
| before | `order-5xx-low` | ok | - | 0.01 | 0 | 0.0 s | 9/9 | `(sum(rate(http_server_request_duration_seconds_count{service_name="order-service", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="order-service"}[1m]))` |
| before | `frontend-5xx-low` | ok | - | 0.01 | 0 | 0.0 s | 9/9 | `(sum(rate(http_server_request_duration_seconds_count{service_name="frontend", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="frontend"}[1m]))` |
| during | `order-pool-pending` | ok | - | 0 | 42 | 15.0 s | 20/21 | `max(max_over_time(db_client_connection_pending_requests{service_name="order-service"}[30s]))` |
| during | `order-pool-saturated` | ok | - | 10 | 10 | 15.0 s | 20/21 | `max(max_over_time(db_client_connection_count{service_name="order-service", db_client_connection_state="used"}[30s]))` |
| during | `order-pool-timeout-logs` | ok | - | 0 | 386 | 14.8 s | 21/21 | `sum(count_over_time({service_name="order-service"} \|= "Timed out waiting for a database connection" [1m]))` |
| during | `order-5xx-share` | ok | - | 0.2 | 0.6751 | 30.0 s | 19/21 | `(sum(rate(http_server_request_duration_seconds_count{service_name="order-service", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="order-service"}[1m]))` |
| during | `frontend-5xx` | ok | - | 0.05 | 0.4074 | 30.0 s | 19/21 | `(sum(rate(http_server_request_duration_seconds_count{service_name="frontend", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="frontend"}[1m]))` |
| during | `order-error-traces` | ok | - | 5 | 50 | 285.8 s | 50/50 | `jaeger {"service": "order-service", "tags": {"http.response.status_code": "503"}}` |
| during | `gateway-error-traces` | ok | - | 5 | 50 | 274.1 s | 50/50 | `jaeger {"service": "api-gateway", "tags": {"http.response.status_code": "502"}}` |
| during | `frontend-error-traces` | ok | - | 5 | 50 | 281.1 s | 50/50 | `jaeger {"service": "frontend", "tags": {"http.response.status_code": "502"}}` |
| during | `payment-unaffected` | ok | - | 0.01 | 0 | 0.0 s | 21/21 | `(sum(rate(http_server_request_duration_seconds_count{service_name="payment-service", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="payment-service"}[1m]))` |
| after | `order-pool-idle` | ok | - | 1 | 0 | 0.0 s | 7/7 | `max(avg_over_time(db_client_connection_pending_requests{service_name="order-service"}[1m]))` |
| after | `order-5xx-low` | ok | - | 0.01 | 0 | 0.0 s | 7/7 | `(sum(rate(http_server_request_duration_seconds_count{service_name="order-service", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="order-service"}[1m]))` |
| after | `frontend-5xx-low` | ok | - | 0.01 | 0 | 0.0 s | 7/7 | `(sum(rate(http_server_request_duration_seconds_count{service_name="frontend", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="frontend"}[1m]))` |


### 2. `slow-sql-missing-index` — Slow order-history query after its indexes are dropped

- **Trigger:** SQL change drop-order-history-indexes. A schema change drops ix_orders_user_id_created_at and ix_orders_created_at on the 500k-row orders table. GET /orders (a user's newest orders) has no index left to use, so every order-history lookup scans the table, and the account page that shows it slows down with it. Nothing errors.
- **Root cause:** slow_query · order-service — The order-history query filters orders by user_id and sorts by created_at; with neither the composite index nor the created_at index it scans the 500k-row table on every request, so GET /orders p95 latency rises by orders of magnitude and the frontend account page slows with it. No errors are returned.
- **Component:** orders table: missing indexes ix_orders_user_id_created_at (user_id, created_at DESC) and ix_orders_created_at (created_at) · code location: `services/shop/order_service/db.py` · `ix_orders_user_id_created_at`
- **Affected:** order-service, api-gateway, frontend · **unaffected:** payment-service, user-service
- **Stop:** CREATE both indexes again

Run `20261117T204818Z-99e367` (seed 42, windows 120/300/180 s): **verified**. Fault 2026-11-17T20:50:18+00:00 → 2026-11-17T20:55:25+00:00.

| phase | check | result | baseline | threshold | peak | onset | points met | query |
|---|---|---|---|---|---|---|---|---|
| before | `orders-p95-normal` | ok | 0.008896 | 0.05 | 0.009196 | 0.0 s | 9/9 | `histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket{service_name="order-service", http_route="/orders", http_request_method="GET"}[1m])))` |
| before | `order-5xx-low` | ok | - | 0.01 | 0 | 0.0 s | 9/9 | `(sum(rate(http_server_request_duration_seconds_count{service_name="order-service", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="order-service"}[1m]))` |
| during | `orders-p95-slow` | ok | 0.008896 | 0.05 | 2.425 | 15.0 s | 20/21 | `histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket{service_name="order-service", http_route="/orders", http_request_method="GET"}[1m])))` |
| during | `account-p95-slow` | ok | 0.04392 | 0.1318 | 2.425 | 15.0 s | 20/21 | `histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket{service_name="frontend", http_route="/account", http_request_method="GET"}[1m])))` |
| during | `orders-slow-select-spans` | ok | - | 5 | 50 | 256.7 s | 50/50 | `jaeger {"service": "order-service", "operation": "SELECT", "min_duration_ms": 500}` |
| during | `orders-no-error-spike` | ok | - | 0.01 | 0 | 0.0 s | 21/21 | `(sum(rate(http_server_request_duration_seconds_count{service_name="order-service", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="order-service"}[1m]))` |
| after | `orders-p95-normal` | ok | 0.008896 | 0.05 | 0.00905 | 0.0 s | 7/7 | `histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket{service_name="order-service", http_route="/orders", http_request_method="GET"}[1m])))` |
| after | `order-5xx-low` | ok | - | 0.01 | 0 | 0.0 s | 7/7 | `(sum(rate(http_server_request_duration_seconds_count{service_name="order-service", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="order-service"}[1m]))` |


### 3. `failed-deployment-bad-config` — Failed deployment: user-service 2.4.0 with a bad database config

- **Trigger:** deploy user-service 2.4.0 with DATABASE_NAME=users_v2. The deploy pipeline releases user-service 2.4.0 whose config sets DATABASE_NAME=users_v2, a database that does not exist. Deploy events are emitted. Every new user-service process fails at startup and exits; Docker restarts it, so it crash-loops. Logins and session checks through the gateway fail.
- **Root cause:** bad_deployment · user-service — Release 2.4.0 of user-service points its database at users_v2, which does not exist, so every instance fails during startup and the container crash-loops; api-gateway's login and session calls to user-service fail, so login and account pages return errors. Rolling back to 2.3.0 fixes it.
- **Component:** release 2.4.0 config DATABASE_NAME=users_v2 (database does not exist) · code location: none (configuration / network)
- **Affected:** user-service, api-gateway, frontend · **unaffected:** order-service, payment-service
- **Stop:** roll back: deploy user-service 2.3.0 with its original config (a second pair of deploy events)

Run `20261117T201435Z-872786` (seed 42, windows 120/300/180 s): **verified**. Fault 2026-11-17T20:16:36+00:00 → 2026-11-17T20:22:47+00:00.

| phase | check | result | baseline | threshold | peak | onset | points met | query |
|---|---|---|---|---|---|---|---|---|
| before | `gateway-auth-5xx-low` | ok | - | 0.01 | 0 | 0.0 s | 9/9 | `(sum(rate(http_server_request_duration_seconds_count{service_name="api-gateway", http_route=~"/api/login\|/api/account", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="api-gateway", http_route=~"/api/login\|/api/account"}[1m]))` |
| before | `user-service-serving` | ok | - | 0.5 | 4.998 | 0.0 s | 9/9 | `sum(rate(http_server_request_duration_seconds_count{service_name="user-service"}[1m])) or vector(0)` |
| during | `bad-deploy-event` | ok | - | 0 | 2 | 9.0 s | 7/7 | `sum(count_over_time({service_name="deploy-pipeline"} \| deployment_service_name="user-service" \| deployment_version="2.4.0" [1m]))` |
| during | `user-startup-failure-logs` | ok | - | 0 | 8 | 9.0 s | 24/24 | `sum(count_over_time({service_name="user-service"} \|= "users_v2" [1m]))` |
| during | `user-instance-churn` | ok | - | 3 | 12 | 9.0 s | 24/24 | `count(count by (service_instance_id) (count_over_time({service_name="user-service"} \|= "service starting" [5m])))` |
| during | `gateway-auth-5xx` | ok | - | 0.5 | 1 | 45.0 s | 21/24 | `(sum(rate(http_server_request_duration_seconds_count{service_name="api-gateway", http_route=~"/api/login\|/api/account", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="api-gateway", http_route=~"/api/login\|/api/account"}[1m]))` |
| during | `frontend-account-5xx` | ok | - | 0.2 | 1 | 15.0 s | 23/24 | `(sum(rate(http_server_request_duration_seconds_count{service_name="frontend", http_route=~"/login\|/account", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="frontend", http_route=~"/login\|/account"}[1m]))` |
| during | `gateway-auth-error-traces` | ok | - | 5 | 50 | 230.2 s | 50/50 | `jaeger {"service": "api-gateway", "operation": "POST /api/login", "tags": {"http.response.status_code": "503"}}` |
| during | `catalogue-unaffected` | ok | - | 0.01 | 0 | 0.0 s | 24/24 | `(sum(rate(http_server_request_duration_seconds_count{service_name="frontend", http_route=~"/products\|/products/{sku}", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="frontend", http_route=~"/products\|/products/{sku}"}[1m]))` |
| after | `gateway-auth-5xx-low` | ok | - | 0.01 | 0 | 0.0 s | 7/7 | `(sum(rate(http_server_request_duration_seconds_count{service_name="api-gateway", http_route=~"/api/login\|/api/account", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="api-gateway", http_route=~"/api/login\|/api/account"}[1m]))` |
| after | `user-service-serving` | ok | - | 0.5 | 5.218 | 0.0 s | 7/7 | `sum(rate(http_server_request_duration_seconds_count{service_name="user-service"}[1m])) or vector(0)` |


### 4. `payment-unavailable` — payment-service unreachable from order-service

- **Trigger:** Toxiproxy link order-to-payment disabled. The network link from order-service to payment-service (through Toxiproxy) is disabled, so connections are refused. Checkout needs a payment authorisation and fails; browsing does not touch payment-service and keeps working.
- **Root cause:** dependency_unavailable · payment-service — order-service cannot connect to payment-service, so payment authorisation fails and checkout returns errors through api-gateway and frontend; payment-service itself receives no traffic, and catalogue browsing is unaffected.
- **Component:** network path order-service -> payment-service (connection refused) · code location: none (configuration / network)
- **Affected:** payment-service, order-service, api-gateway, frontend · **unaffected:** user-service
- **Stop:** re-enable the order-to-payment link

Run `20261117T202618Z-2a9db3` (seed 42, windows 120/300/180 s): **verified**. Fault 2026-11-17T20:28:18+00:00 → 2026-11-17T20:33:18+00:00.

| phase | check | result | baseline | threshold | peak | onset | points met | query |
|---|---|---|---|---|---|---|---|---|
| before | `checkout-5xx-low` | ok | - | 0.01 | 0 | 0.0 s | 9/9 | `(sum(rate(http_server_request_duration_seconds_count{service_name="api-gateway", http_route=~"/api/orders", http_request_method="POST", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="api-gateway", http_route=~"/api/orders", http_request_method="POST"}[1m]))` |
| before | `payment-serving` | ok | - | 0.2 | 0.9997 | 0.0 s | 9/9 | `sum(rate(http_server_request_duration_seconds_count{service_name="payment-service"}[1m])) or vector(0)` |
| during | `order-payment-unavailable-logs` | ok | - | 0 | 71 | 11.9 s | 21/21 | `sum(count_over_time({service_name="order-service"} \|= "payment-service is unavailable" [1m]))` |
| during | `order-payment-connect-error-spans` | ok | - | 5 | 50 | 244.7 s | 50/50 | `jaeger {"service": "order-service", "operation": "POST", "tags": {"error.type": "ConnectError"}}` |
| during | `checkout-5xx` | ok | - | 0.5 | 1 | 45.0 s | 18/21 | `(sum(rate(http_server_request_duration_seconds_count{service_name="api-gateway", http_route=~"/api/orders", http_request_method="POST", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="api-gateway", http_route=~"/api/orders", http_request_method="POST"}[1m]))` |
| during | `payment-no-traffic` | ok | - | 0.05 | 0.9397 | 60.0 s | 17/21 | `sum(rate(http_server_request_duration_seconds_count{service_name="payment-service"}[1m])) or vector(0)` |
| during | `order-checkout-error-traces` | ok | - | 5 | 50 | 244.7 s | 50/50 | `jaeger {"service": "order-service", "operation": "POST /orders", "tags": {"http.response.status_code": "503"}}` |
| during | `browsing-unaffected` | ok | - | 0.01 | 0 | 0.0 s | 21/21 | `(sum(rate(http_server_request_duration_seconds_count{service_name="frontend", http_route=~"/products\|/products/{sku}", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="frontend", http_route=~"/products\|/products/{sku}"}[1m]))` |
| after | `checkout-5xx-low` | ok | - | 0.01 | 0 | 0.0 s | 7/7 | `(sum(rate(http_server_request_duration_seconds_count{service_name="api-gateway", http_route=~"/api/orders", http_request_method="POST", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="api-gateway", http_route=~"/api/orders", http_request_method="POST"}[1m]))` |
| after | `payment-serving` | ok | - | 0.2 | 1.04 | 0.0 s | 7/7 | `sum(rate(http_server_request_duration_seconds_count{service_name="payment-service"}[1m])) or vector(0)` |


### 5. `memory-leak` — Memory leak in api-gateway's response cache (bounded by its cgroup)

- **Trigger:** traffic mix cache-busting-client. A client adds a unique, seeded ?v=<nonce> to product-list calls at a fixed rate. api-gateway caches catalogue responses by full URL and never evicts expired entries, so every unique URL stays in memory. The process grows steadily until the container reaches its 192 MB limit (no swap) and is OOM-killed and restarted; it then starts filling again.
- **Root cause:** memory_leak · api-gateway — api-gateway's catalogue response cache stores one entry per distinct URL and only overwrites an entry when the same URL is requested again, so a client that varies a query parameter makes it grow without bound until the container is OOM-killed.
- **Component:** catalogue ResponseCache (keyed by full URL, no size bound or eviction) · code location: `services/shop/api_gateway/cache.py` · `ResponseCache.put`
- **Affected:** api-gateway, frontend · **unaffected:** order-service, payment-service, user-service
- **Stop:** traffic mix back to baseline, then restart api-gateway to free the cache (part of recovery)

Run `20261117T205855Z-b06df8` (seed 42, windows 120/300/180 s): **verified**. Fault 2026-11-17T21:00:56+00:00 → 2026-11-17T21:05:56+00:00.

| phase | check | result | baseline | threshold | peak | onset | points met | query |
|---|---|---|---|---|---|---|---|---|
| before | `gateway-memory-normal` | ok | 81.3 MiB | 105.7 MiB | 81.3 MiB | 0.0 s | 9/9 | `max(max_over_time(process_memory_usage_bytes{service_name="api-gateway"}[30s]))` |
| before | `frontend-5xx-low` | ok | - | 0.01 | 0 | 0.0 s | 9/9 | `(sum(rate(http_server_request_duration_seconds_count{service_name="frontend", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="frontend"}[1m]))` |
| during | `gateway-memory-growth` | ok | - | 2e+05 | 6.564e+05 | 30.0 s | 19/21 | `max(deriv(process_memory_usage_bytes{service_name="api-gateway"}[1m]))` |
| during | `gateway-near-limit` | ok | - | 0.85 | 0.9304 | 180.0 s | 4/21 | `max(max_over_time(container_memory_usage_bytes{service_name="api-gateway"}[30s]) / max_over_time(container_memory_limit_bytes{service_name="api-gateway"}[30s]))` |
| during | `gateway-restarted` | ok | - | 0 | 1 | 214.0 s | 4/4 | `sum(count_over_time({service_name="api-gateway"} \|= "service starting" [1m]))` |
| during | `frontend-5xx-blip` | ok | - | 0.02 | 0.03287 | 225.0 s | 2/21 | `(sum(rate(http_server_request_duration_seconds_count{service_name="frontend", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="frontend"}[1m]))` |
| during | `order-service-unaffected` | ok | - | 0.01 | 0 | 0.0 s | 21/21 | `(sum(rate(http_server_request_duration_seconds_count{service_name="order-service", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="order-service"}[1m]))` |
| after | `gateway-memory-normal` | ok | 81.3 MiB | 105.7 MiB | 79.6 MiB | 0.0 s | 7/7 | `max(max_over_time(process_memory_usage_bytes{service_name="api-gateway"}[30s]))` |
| after | `frontend-5xx-low` | ok | - | 0.01 | 0 | 0.0 s | 7/7 | `(sum(rate(http_server_request_duration_seconds_count{service_name="frontend", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="frontend"}[1m]))` |
