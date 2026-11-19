# Progress log

Read at the start of every session. Updated at the end of each phase.

## Phase 3 — in progress (branch `phase-3-incident-management`)

Plan: `docs/plans/phase-3.md`, approved 2026-11-19 (D1–D6 as proposed). Work runs in the Codespace
(4 cores, 15 GiB); SentinelAI's Postgres/Redis run next to the testbed.

| Task | Status |
|---|---|
| T1 Registry | done — see below |
| T2 Telemetry storage | done — see below |
| T3 OTLP endpoint | done — see below |
| T4 Ingest writer | done — see below |
| T5 Testbed wiring (live) | done — see below |
| T6 Dependency graph | not started |
| T7 Detection | not started |
| T8 Correlation + incidents | not started |
| T9 UI | not started |
| T10 Live acceptance + benchmarks | not started — **stop and report** after the S1 run and after the 10-min quiet baseline |
| T11 Docs + report | not started |

### T1 Registry (2026-11-19)
- Migration `0002`: `services`, `service_dependencies`, `service_api_keys`; `projects.archived_at`.
  Composite FKs `(org_id, parent_id)` on every child (DB-level tests prove cross-tenant inserts fail).
- `/api/v1/orgs/{org_id}/services` CRUD (DELETE = archive), `PUT …/dependencies` (replace set; same
  project, active targets only), `…/api-keys` list/create/revoke (admin). Keys `sai_<prefix>_<secret>`,
  SHA-256 at rest, plaintext once (`Cache-Control: no-store`), max 10 active per service, audit rows
  carry the prefix only. Repo URLs with credentials are rejected.
- Project DELETE now archives: project read-only (409 `project_archived`), its services archived and
  their keys revoked in one transaction; lists hide archived unless `include_archived=true`.
- ADR 0003 guard: `tests/unit/test_integrity_boundary.py` fails if `backend/app|migrations|scripts`
  mention ground truth, the harness, run records or any scenario id (mutation-checked: planting
  `db-pool-exhaustion` in a comment made it fail).
- Verified: ruff + format clean, mypy strict clean (100 files), **229 backend tests passed** (160
  before T1); OpenAPI-driven isolation guard now covers the new routes (≥ 20 org-scoped operations);
  frontend types regenerated, typecheck/lint/12 tests/build pass. `EXPLAIN` (seqscan disabled) shows
  every new list query using an `org_id`-prefixed index; registry tables are small (tens–hundreds of
  rows per org), so the remaining filters were not given dedicated indexes.
- Limitations: no project/service un-archive endpoint; the 10-active-keys limit is not race-proof
  under concurrent creates (soft limit); key `last_used_at` is written from T3 on.

### T2 Telemetry storage (2026-11-19)
- Migration `0003`: day-partitioned `spans`, `log_records`, `metric_points`; `metric_series`,
  `deployments`, `ingest_usage`. Dedup primary keys (include the partition column), composite
  tenant FKs to `services`/`metric_series`, id-length and enum CHECKs. ADR 0005 written.
- `app.services.telemetry_maintenance`: keeps partitions `today-1 … today+2`, drops days older than
  retention (spans/logs 3 d, metrics 7 d; usage rows 30 d), advisory-lock guarded. Runs from the
  `migrate` container (`python -m app.cli.telemetry_maintenance`), from the new **`beat`** container
  every 10 min (task `telemetry.maintain_partitions`), and from ingest start-up in T3.
- Verified: 240 backend tests pass (11 new storage tests: window + idempotency, routing into the
  day partition, re-insert dedup, out-of-window insert fails, per-table retention, lock skip, usage
  retention, cross-tenant FK, id lengths, JIT off); `alembic check` clean; image built; `migrate`
  container ran migrations + maintenance; worker executed the task from the image (0.14 s); beat
  started. Beat's first scheduled fire observed later: `Sending due task telemetry-maintain-partitions`
  at 11:13:53 UTC, worker succeeded.
- EXPLAIN on 1M seeded spans / 300k logs / 72k points (`docs/evidence/phase-3/t2-explain.txt`): every
  §3.2 query uses its intended index. Found: Postgres JIT made the graph query 733 ms vs 16.7 ms
  without (rollup 9.0 → 0.9 ms) → `jit=off` on SentinelAI's connections (tested).
- Plan deviation: the maintenance CLI lives in `app/cli/` (not `scripts/`), because
  `backend/.dockerignore` keeps dev scripts out of the image.

### T3 OTLP endpoint (2026-11-19)
- Separate **`ingest`** process/container (`app.ingest_main`, port 8001, `mem_limit 384m`):
  `POST /api/v1/otlp/v1/{traces,logs,metrics}`, OTLP protobuf or JSON (hex ids), gzip or identity.
  Order: body cap (4 MiB) → key auth (`sai_…`, 10 s cache) → per-key requests/s (50) → decode
  (gunzip capped at 16 MiB) → records per request (10,000) → per-key records/s (20,000) →
  per-record validation/normalization (in a worker thread) → bounded enqueue → OTLP response with
  `partial_success`. Codes: 400 malformed, 401 key, 413 size/records, 415 media type, 429 rate
  (Retry-After), 503 queue full / Redis down (Retry-After 5).
- Validation drops and counts per reason (ids, timestamps outside `now-2h … now+5min`, names,
  kinds, metric values, unsupported metric types, `service_mismatch`). Attributes bounded (≤ 32
  keys, values ≤ 256 chars) with **secret-looking keys redacted at ingest**. Org/service always come
  from the key. Deploy events (`event.name=deployment`, emitted by `deploy-pipeline`) are accepted on
  the key of the service named in `deployment.service.name` and become `deployments` rows.
- Migration `0004`: `metric_series.temporality`. Dependencies: `opentelemetry-proto` (protobuf
  decode); dev `types-protobuf` (mypy stubs). ADR 0002 documents the key-lookup exception.
- Verified: **294 backend tests pass** (+54: 20 ingest API integration — protobuf/JSON/gzip, partial
  success, malformed → 400 + counted + process keeps serving, cross-tenant/service mismatch,
  401 for missing/revoked/archived, 429 requests and records, 413 records/body/gzip bomb, 415, 503
  with Retry-After, real Celery/Redis queue bound; unit tests for decode, attributes, normalization,
  wire format, key cache/revocation). ruff/mypy strict clean (127 files).
- **Live overload** (`docs/evidence/phase-3/t3-overload.md`, no writer consuming): 6,091 requests
  in 40 s from 16 senders → 109 × 200, 5,278 × 429, 704 × 503, 0 client errors; ingest memory
  78.7 → peak 126.7 MiB of 384 MiB, 0 restarts, not OOM-killed.
- Plan deviation: the queue bound is **max pending batches (LLEN of the broker list, default 100)
  + max normalized batch size (2 MiB)** instead of a byte-total counter — same bound, but it cannot
  drift if a writer dies. Check-then-send may overshoot by the number of in-flight requests
  (observed: 109 vs 100).
- Limitations: rejections are written to `ingest_usage` by the T4 writer (best effort for
  malformed requests when the queue is full; Prometheus counts always); no per-IP limit on failed
  auth (the ingest port binds to 127.0.0.1 / the compose network).

### T4 Ingest writer (2026-11-19)
- New **`ingest-writer`** container (Celery worker on the `ingest` queue only, `mem_limit 512m`),
  per-process event loop + small pool. One transaction per batch: claim `batch_id` in
  `ingest_batches` (migration `0005`; a redelivered message is skipped, so usage counts are exact),
  `INSERT … ON CONFLICT DO NOTHING` for spans/logs/deployments/points (chunked under the bind
  limit), metric cardinality guard (2,000 series/service → `series_limit`), per-minute
  `ingest_usage` upsert, throttled `services.last_seen_at` / `service_api_keys.last_used_at`.
- Failures: retry with exponential backoff + ±50% jitter (≤ 5 retries, cap 60 s), then a capped
  Redis dead-letter list `ingest:dead` (1,000) + `ingest_dead_letters_total`; undecodable payloads
  go straight to dead-letter.
- `TenantRepository` now takes a `TenantScope` (`TenantContext` for users, `SystemScope` for
  workers whose org comes from a verified key).
- Fix found while planning the live check: DB down + uncached key returned 500 → now 503 +
  Retry-After.
- Verified: **312 backend tests pass** (writer: every signal + usage + bookkeeping, redelivered
  batch has no effect, re-sent records in a new batch not duplicated, cardinality guard,
  rejection-only usage, forged cross-tenant batch refused by FK, **end-to-end duplicate OTLP request
  → 10 rows not 20 and malformed counted**, dead-letter cap; task retry/dead-letter policy).
  **Live** (`docs/evidence/phase-3/t4-writer-live.md`): duplicate batch → no duplicates; Postgres
  stopped 12.5 s → writer retried 4× and the batch landed (100/100 spans); uncached key with DB
  down → 503.

### T5 Testbed wiring (2026-11-19, live)
- Shared Docker network `sentinelai-ingest` (created by SentinelAI's compose; `ingest` has alias
  `sentinelai-ingest`); testbed overlay `docker-compose.sentinelai.yml`, added by `tb.sh` when
  `SENTINELAI_OTLP_ENDPOINT` is set. Collector overlay: `routing` connectors → one `otlphttp`
  exporter per service with that service's key; deploy events routed by
  `deployment.service.name` + `deployment.id`; exporter queues bounded at 8 MiB each.
- `make sentinel-connect [REGISTER=1]` (`backend/scripts/connect_testbed.py`): applies
  `testbed/sentinelai-registry.json` via the public API (idempotent), rotates per-service keys,
  writes `testbed/.env`. Registry holds no scenario information (integrity guard passes).
- **Verified live** (`docs/evidence/phase-3/t5-testbed-wiring.md`): rows from all 5 services for
  spans, logs and metrics, 0 rejections; deploy round trip → 4 `deployments` rows (after fixing the
  route: the first round trip's 4 events were never delivered — the emitter's handler puts the
  event name in `LogRecord.event_name`, not an attribute; unit test added). Freshness lag p50
  3.71 s / p95 6.68 s / max 8.48 s (upper bound on pipeline lag). Storage ~196 MiB per hour of
  baseline (~268k spans/h).
- Retention: at that rate 3-day spans/logs approach this Codespace's free disk (16 GiB); default
  unchanged, this Codespace's `.env` uses 1 day, and compose now passes `TELEMETRY_RETENTION_*`.
- Checks: 313 backend tests, testbed `make testbed-test` 125 passed (unchanged), ruff/mypy clean.

## Phase 2 report (2026-11-17) — testbed

All live work ran in the Codespace (4 cores, 16 GB; ADR 0004). Branch `phase-2-testbed`; Phase 3 not
started.

### Implemented
- **T1–T4** (earlier): five-service shop, deterministic seed (500k orders), OTel → Collector →
  Prometheus / Loki / Jaeger v2, Grafana on demand.
- **T5** seeded load generator + mixes, deploy pipeline with deploy events; `harness.live` checks.
- **T6** fault controller: plugins over allow-listed primitives (traffic mix, Toxiproxy link,
  named SQL change, ops restart, deploy), Prometheus/Loki/Jaeger symptom checks with saved
  evidence, run lifecycle with lock, memory guard, stop requests and always-remove-fault, CLI +
  token-protected HTTP API, ground-truth JSON schema, run records, fingerprints.
- **T7/T8** five scenarios with definitions, plugins and live-calibrated checks.
- **T9** same-seed reproducibility runs; `testbed-scenarios.yml`; scenarios in `testbed-live.yml`.
- **T10** `docs/testbed.md`, `testbed/README.md`, `harness.evidence` (committable run evidence).

### Verified live (each claim has a run id; records in `docs/evidence/phase-2/`)
| What | Result | Run ids |
|---|---|---|
| 10-min baseline | PASS: 5,958 req, 9.93 req/s, 0 errors, 5/5 complete checkout traces | `20261117T184537Z-c99f0a` |
| Deploy round-trip | PASS: 1.2.0 → 1.2.1 in 3.1 s, events in Loki, rollback ok | `20261117T154229Z-719b99` |
| Controller dry run | schema-valid record, fault not injected, 15/15 evidence files present | `20261117T185940Z-8ec55c` |
| Controller HTTP auth | 401 without / with wrong token, 200 with token | — |
| S1 `db-pool-exhaustion` | VERIFIED ×3 | `…192849Z-e86599`, `…194200Z-892d6a`, `…210948Z-cbfdb8` |
| S2 `slow-sql-missing-index` | VERIFIED ×2 (after one failure) | `…204818Z-99e367`, `…212020Z-b68c81` |
| S3 `failed-deployment-bad-config` | VERIFIED ×2 | `…201435Z-872786`, `…213058Z-58a894` |
| S4 `payment-unavailable` | VERIFIED ×2 | `…202618Z-2a9db3`, `…214241Z-54338b` |
| S5 `memory-leak` | VERIFIED ×2 (after one failure) | `…205855Z-b06df8`, `…215313Z-802a22` |
| Same seed → same pattern | 5/5 pairs equal within tolerance | T9 section; evidence for all 10 runs in `docs/evidence/phase-2/` |
| Jaeger memory under S1 | peak 266.5 / 384 MiB, 0 restarts | `…194200Z-892d6a` |

### Not verified
- **`testbed-scenarios.yml` and the scenario path of `testbed-live.yml` have never run on GitHub.**
  Both pass actionlint/shellcheck locally. Manual dispatch needs the files on `main`; the nightly
  schedule only fires from the default branch.
- **Reproducibility on a fresh testbed** (e.g. a CI runner): every run here used one long-running
  stack. Orders accumulate (each checkout adds one), so S1's 7-day report grows over time and
  S2's effect depended on recent orders — results on a fresh stack may differ in magnitude.
- **Fingerprints are equal only at the tolerances in the T9 section; one of them was set after
  seeing data** (magnitude ±1; see Decisions). At the plan's original ±0, S1's repeat fails.
- Backend/frontend unchanged since Phase 1 (`git diff origin/main...HEAD -- backend frontend` is
  empty). This session: backend ruff + mypy clean, 88 DB-free tests pass, 72 DB tests not run
  (SentinelAI's Postgres is stopped while the testbed runs); frontend not run (no `node_modules`).
  CI runs both with their services.
- Grafana (`make testbed-ui`) was not exercised in this session.

### Tests
`make testbed-test`: ruff format + check clean, mypy --strict clean (71 files), **125 passed**
(5 integration/scenario tests deselected; they need the live stack and were covered by the runs
above). actionlint 1.7.7: clean.

### Decisions (and why) — plan deviations are marked in `docs/plans/phase-2.md`
- **Jaeger 384 MiB / 2,000 traces** (plan: 256 MiB / 10,000): 256 MiB restarted mid-fault at both
  10,000 and 3,000 traces. Testbed limits now total 2,640 MiB long-running.
- **S2 drops both order-history indexes** (plan: one): with only the composite index gone, p95
  rose 8 → 10–16 ms because the planner walks `ix_orders_created_at`.
- **Magnitude tolerance ±1 log2 bucket** (plan: ±0), **set after observing** two S1 runs at 31% and
  41% peak frontend 5xx (log2 ratios 2.64 / 3.03). A ≥2-bucket (≥4×) difference still fails.
- **Trace-check onsets excluded from fingerprints**: they are the earliest of the newest 50
  traces (273 s vs a real 15 s onset).
- **Trace checks match HTTP status / `error.type`**: Jaeger 2.12's memstore cannot search `error`.
- **Recovery window 180 s** (plan: 120 s): judged on the second half, which must clear the checks'
  1-minute lookbacks.
- **Cross-instance maxima use `max_over_time(...[30s])`**: Prometheus holds a dead instance's last
  sample for 5 minutes.
- **S5 stream 45/s** (plan: calibrate to 3–4 min): 20/s grew the gateway 0.26 MiB/s; 45/s →
  OOM at 196–214 s.
- **Evidence**: Prometheus/Loki responses committed verbatim; Jaeger as digests with SHA-256 (raw
  1–2 MB each).

### Known limitations
- Data drift (above); `make testbed-down CLEAN=1` + `up` resets it.
- S2's p95 tops out at the histogram's 2.5 s bucket; the true scan time may be higher.
- Running `make testbed-up` during a scenario kills it without cleanup (documented).
- Toxiproxy covers one link (order → payment); SQL changes are one named pair.
- The S3 scenario leaves the release manifest pinned to user-service 2.3.0 (same as the image).

### Files changed this phase (summary)
`testbed/` (services, harness incl. `controller/` and `evidence.py`, scenarios + schema, infra,
compose, tests), `.github/workflows/testbed-live.yml`, `.github/workflows/testbed-scenarios.yml`,
`docs/testbed.md`, `docs/evidence/phase-2/`, `docs/plans/phase-2.md`, ADRs 0003–0004,
`docs/codespaces.md`, `.devcontainer/`, `Makefile`, this file.

### Remaining / next
- Merge decision (PR #1) — then dispatch `testbed-scenarios.yml` once to verify CI live.
- Open item carried from the handoff: commit author emails are personal; hiding them needs a
  history rewrite. Not decided.
- **Stop here; Phase 3 waits for your go-ahead.**

### Operational notes
- Codespace forwarded ports: 8081 shop, 8089 loadgen, 8090 controller, 9090 Prometheus,
  3100 Loki, 16686 Jaeger, 3001 Grafana. Use `127.0.0.1`, not `localhost`.
- A Codespace built from a broken config does not fix itself: delete it or run a full rebuild.
- Never call anything verified that did not run live; stop the Codespace when idle.

| Phase | Status | Notes |
|---|---|---|
| 1. Foundation | **Complete, merged to main (2026-11-17)** | Plan: `docs/plans/phase-1.md`. |
| 2. Testbed | **Complete, merged to main (PR #1)** | Plan: `docs/plans/phase-2.md`; ADR 0003 accepted; live verification moved to Codespaces/CI (ADR 0004, `docs/codespaces.md`). |
| 3. Incident management | **In progress** — T1–T5 done | Plan: `docs/plans/phase-3.md` (approved 2026-11-19). |
| 4. Investigation engine | Not started | |
| 5. Recovery | Not started | |
| 6. Hardening + evaluation | Not started | |

## Phase 1 task log

| Task | Status |
|---|---|
| T1 Repo skeleton | done |
| T2 Backend core | done |
| T3 Compose stack | done |
| T4 Data model + migration | done |
| T5 Auth | done |
| T6 Orgs / RBAC / projects | done |
| T7 Readiness | done |
| T8 Frontend | done |
| T9 CI | done (validated locally; first GitHub run green on PR #1, 2026-11-17) |
| T10 Docs + report | done |

## Phase 2 task log

| Task | Status |
|---|---|
| T1 Skeleton (uv project, compose, limits, Makefile + tb.sh) | done |
| T2 Services | done |
| T3 Telemetry + Collector | done |
| T4 Backends | done |
| T5 Loadgen + deploy emitter | done — **verified live in Codespace (2026-11-17)**: baseline run `20261117T184537Z-c99f0a` PASS (10 min, 5958 requests, 9.93 req/s, 0 errors = 0.000%, 5/5 settled checkout traces span all five services, all 5 services in Prometheus/Jaeger/Loki); deploy-check run `20261117T154229Z-719b99` PASS (payment-service 1.2.0→1.2.1 in 3.1 s, version label in Prometheus, both deploy events in Loki, rollback ok). First baseline (`20261117T153044Z-ec58e2`) failed: Jaeger was queried before the newest traces were fully exported; fixed by only reading traces that ended ≥ 30 s ago |
| T6 Controller + ground truth | done — **verified live (2026-11-17)**: controller container healthy; `scenario list/validate` find S1 definition + plugin; HTTP API 401 without/with wrong token, 200 with token; dry run `20261117T185940Z-8ec55c` (S1, seed 42, 120/60/60 s) wrote a run record that validates against `ground-truth.schema.json` (0 errors, checked independently on the host), cites 15 evidence files (all present), fault NOT injected, no S1 symptoms observed, baseline + recovery healthy. 119 unit tests pass |
| T7 S1 pool exhaustion | done — **verified live (2026-11-17)**: run `20261117T192849Z-e86599` VERIFIED, 9/9 symptoms + 3/3 recovery checks; evidence in `docs/evidence/phase-2/s1-20261117T192849Z-e86599/` and the T7 section below. Jaeger restarted once mid-fault (memory) — fix in the next commit |
| T8 S2–S5 | done — **verified live (2026-11-17)**: S2 `20261117T204818Z-99e367`, S3 `20261117T201435Z-872786`, S4 `20261117T202618Z-2a9db3`, S5 `20261117T205855Z-b06df8` all VERIFIED; S2 and S5 failed once first (causes + fixes in the T8 section). Evidence: `docs/evidence/phase-2/<scenario>-<run_id>/` |
| T9 Reproducibility + CI | done — **verified live**: one same-seed (42) repeat per scenario, all 5 VERIFIED, all 5 fingerprints equal within ±1 onset / ±1 magnitude bucket (T9 section). CI: `testbed-scenarios.yml` (dispatch + nightly, x2 per scenario + compare) and scenario support in `testbed-live.yml` — actionlint-clean, **never run on GitHub** |
| T10 Docs + report | done — `docs/testbed.md` (scenario sections generated from definitions + verified runs), `testbed/README.md`, this report |

## Phase 2 — T7 evidence: S1 DB connection-pool exhaustion

Run `20261117T192849Z-e86599` (seed 42, windows {'baseline_s': 120, 'observe_s': 300, 'recovery_s': 180}): **verified**. Fault 2026-11-17T19:30:49+00:00 → 2026-11-17T19:35:49+00:00. Evidence: `testbed/runs/db-pool-exhaustion/20261117T192849Z-e86599/queries/` (gitignored; raw backend responses).

| phase | check | result | peak | onset | points met | evidence | query |
|---|---|---|---|---|---|---|---|
| before (baseline 120 s) | `order-pool-idle` | ok | 0 | 0s | 9/9 | 001-prometheus.json | `max(avg_over_time(db_client_connection_pending_requests{service_name="order-service"}[1m]))` |
| before (baseline 120 s) | `order-5xx-low` | ok | 0 | 0s | 9/9 | 002-prometheus.json | `(sum(rate(http_server_request_duration_seconds_count{service_name="order-service", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="order-service"}[1m]))` |
| before (baseline 120 s) | `frontend-5xx-low` | ok | 0 | 0s | 9/9 | 003-prometheus.json | `(sum(rate(http_server_request_duration_seconds_count{service_name="frontend", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="frontend"}[1m]))` |
| during (observe 300 s) | `order-pool-pending` | ok | 38 | 15s | 20/21 | 004-prometheus.json | `max(max_over_time(db_client_connection_pending_requests{service_name="order-service"}[30s]))` |
| during (observe 300 s) | `order-pool-saturated` | ok | 10 | 15s | 20/21 | 005-prometheus.json | `max(max_over_time(db_client_connection_count{service_name="order-service", db_client_connection_state="used"}[30s]))` |
| during (observe 300 s) | `order-pool-timeout-logs` | ok | 294 | 10.9s | 21/21 | 006-loki.json | `sum(count_over_time({service_name="order-service"} \|= "Timed out waiting for a database connection" [1m]))` |
| during (observe 300 s) | `order-5xx-share` | ok | 0.5556 | 30s | 19/21 | 007-prometheus.json | `(sum(rate(http_server_request_duration_seconds_count{service_name="order-service", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="order-service"}[1m]))` |
| during (observe 300 s) | `frontend-5xx` | ok | 0.3112 | 30s | 19/21 | 008-prometheus.json | `(sum(rate(http_server_request_duration_seconds_count{service_name="frontend", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="frontend"}[1m]))` |
| during (observe 300 s) | `order-error-traces` | ok | 50 | 278.3s | 50/50 | 009-jaeger.json | `jaeger {"service": "order-service", "tags": {"http.response.status_code": "503"}}` |
| during (observe 300 s) | `gateway-error-traces` | ok | 50 | 272.9s | 50/50 | 010-jaeger.json | `jaeger {"service": "api-gateway", "tags": {"http.response.status_code": "502"}}` |
| during (observe 300 s) | `frontend-error-traces` | ok | 50 | 272.9s | 50/50 | 011-jaeger.json | `jaeger {"service": "frontend", "tags": {"http.response.status_code": "502"}}` |
| during (observe 300 s) | `payment-unaffected` | ok | 0 | 0s | 21/21 | 012-prometheus.json | `(sum(rate(http_server_request_duration_seconds_count{service_name="payment-service", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="payment-service"}[1m]))` |
| after (recovery, last 90 s) | `order-pool-idle` | ok | 0 | 0s | 7/7 | 013-prometheus.json | `max(avg_over_time(db_client_connection_pending_requests{service_name="order-service"}[1m]))` |
| after (recovery, last 90 s) | `order-5xx-low` | ok | 0 | 0s | 7/7 | 014-prometheus.json | `(sum(rate(http_server_request_duration_seconds_count{service_name="order-service", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="order-service"}[1m]))` |
| after (recovery, last 90 s) | `frontend-5xx-low` | ok | 0 | 0s | 7/7 | 015-prometheus.json | `(sum(rate(http_server_request_duration_seconds_count{service_name="frontend", http_response_status_code=~"5.."}[1m])) or vector(0)) / sum(rate(http_server_request_duration_seconds_count{service_name="frontend"}[1m]))` |

Notes (honest limits of this run):
- **Jaeger restarted once during the fault** (19:34:20, 3.5 min in; memory 44 → 247.8 MiB of 256 MiB, sampled every 15 s, then 181 MiB at baseline). The trace checks ran against the traces stored after the restart; they found 50/50 matching traces per service, and every matched order-service trace has `error=true` spans (digest files). Fix and re-measurement: next commit.
- **Trace-check onsets (273–278 s) are not symptom onsets**: they are the earliest of the 50 newest matching traces. Metric/log onsets (11–30 s) are the real ones. Fixed for fingerprints in T9.
- Jaeger evidence is committed as digests (trace ids, span counts, status codes, SHA-256 of the 1.4–1.9 MB raw response); Prometheus/Loki responses are committed verbatim.

How the run got here (all live, same day): run `…190432Z-3a977d` refused to inject (baseline check counted a single pending=1 connection-open blip; check changed to a 1-min average, which peaked at 0.17 over 50 min of baseline); run `…190723Z-51c4b4` observed 6/9 (Jaeger lost traces in a restart; its memstore cannot search the derived `error` tag, so trace checks now match the 503/502 status; recovery points overlapped the stop, so the default recovery window is now 180 s); run `…192356Z-83833b` was killed 8 s after injection by a `testbed-up` recreate (record left as `running`; traffic verified back on baseline). `scenario stop` crashed on a missing `--seed` (fixed, regression test).

## Phase 2 — T8 evidence: S2–S5

Exact queries: `testbed/scenarios/<id>.json` and `docs/testbed.md`. Raw backend responses: `docs/evidence/phase-2/<id>-<run_id>/queries/` (Jaeger as digests). Calibration was done with short manual injections first (`make scenario-start/stop`), all live.

### S2 slow SQL (missing indexes)

Run `20261117T204818Z-99e367` (seed 42, windows 120/300/180 s): **verified**. Fault 2026-11-17T20:50:18+00:00 → 2026-11-17T20:55:25+00:00.

| phase | check | result | baseline | threshold | peak | onset | points met |
|---|---|---|---|---|---|---|---|
| before | `orders-p95-normal` | ok | 0.008896 | 0.05 | 0.009196 | 0.0 s | 9/9 |
| before | `order-5xx-low` | ok | - | 0.01 | 0 | 0.0 s | 9/9 |
| during | `orders-p95-slow` | ok | 0.008896 | 0.05 | 2.425 | 15.0 s | 20/21 |
| during | `account-p95-slow` | ok | 0.04392 | 0.1318 | 2.425 | 15.0 s | 20/21 |
| during | `orders-slow-select-spans` | ok | - | 5 | 50 | 256.7 s | 50/50 |
| during | `orders-no-error-spike` | ok | - | 0.01 | 0 | 0.0 s | 21/21 |
| after | `orders-p95-normal` | ok | 0.008896 | 0.05 | 0.00905 | 0.0 s | 7/7 |
| after | `order-5xx-low` | ok | - | 0.01 | 0 | 0.0 s | 7/7 |

### S3 failed deployment (bad config)

Run `20261117T201435Z-872786` (seed 42, windows 120/300/180 s): **verified**. Fault 2026-11-17T20:16:36+00:00 → 2026-11-17T20:22:47+00:00.

| phase | check | result | baseline | threshold | peak | onset | points met |
|---|---|---|---|---|---|---|---|
| before | `gateway-auth-5xx-low` | ok | - | 0.01 | 0 | 0.0 s | 9/9 |
| before | `user-service-serving` | ok | - | 0.5 | 4.998 | 0.0 s | 9/9 |
| during | `bad-deploy-event` | ok | - | 0 | 2 | 9.0 s | 7/7 |
| during | `user-startup-failure-logs` | ok | - | 0 | 8 | 9.0 s | 24/24 |
| during | `user-instance-churn` | ok | - | 3 | 12 | 9.0 s | 24/24 |
| during | `gateway-auth-5xx` | ok | - | 0.5 | 1 | 45.0 s | 21/24 |
| during | `frontend-account-5xx` | ok | - | 0.2 | 1 | 15.0 s | 23/24 |
| during | `gateway-auth-error-traces` | ok | - | 5 | 50 | 230.2 s | 50/50 |
| during | `catalogue-unaffected` | ok | - | 0.01 | 0 | 0.0 s | 24/24 |
| after | `gateway-auth-5xx-low` | ok | - | 0.01 | 0 | 0.0 s | 7/7 |
| after | `user-service-serving` | ok | - | 0.5 | 5.218 | 0.0 s | 7/7 |

### S4 payment-service unreachable

Run `20261117T202618Z-2a9db3` (seed 42, windows 120/300/180 s): **verified**. Fault 2026-11-17T20:28:18+00:00 → 2026-11-17T20:33:18+00:00.

| phase | check | result | baseline | threshold | peak | onset | points met |
|---|---|---|---|---|---|---|---|
| before | `checkout-5xx-low` | ok | - | 0.01 | 0 | 0.0 s | 9/9 |
| before | `payment-serving` | ok | - | 0.2 | 0.9997 | 0.0 s | 9/9 |
| during | `order-payment-unavailable-logs` | ok | - | 0 | 71 | 11.9 s | 21/21 |
| during | `order-payment-connect-error-spans` | ok | - | 5 | 50 | 244.7 s | 50/50 |
| during | `checkout-5xx` | ok | - | 0.5 | 1 | 45.0 s | 18/21 |
| during | `payment-no-traffic` | ok | - | 0.05 | 0.9397 | 60.0 s | 17/21 |
| during | `order-checkout-error-traces` | ok | - | 5 | 50 | 244.7 s | 50/50 |
| during | `browsing-unaffected` | ok | - | 0.01 | 0 | 0.0 s | 21/21 |
| after | `checkout-5xx-low` | ok | - | 0.01 | 0 | 0.0 s | 7/7 |
| after | `payment-serving` | ok | - | 0.2 | 1.04 | 0.0 s | 7/7 |

### S5 bounded memory leak

Run `20261117T205855Z-b06df8` (seed 42, windows 120/300/180 s): **verified**. Fault 2026-11-17T21:00:56+00:00 → 2026-11-17T21:05:56+00:00.

| phase | check | result | baseline | threshold | peak | onset | points met |
|---|---|---|---|---|---|---|---|
| before | `gateway-memory-normal` | ok | 81.3 MiB | 105.7 MiB | 81.3 MiB | 0.0 s | 9/9 |
| before | `frontend-5xx-low` | ok | - | 0.01 | 0 | 0.0 s | 9/9 |
| during | `gateway-memory-growth` | ok | - | 2e+05 | 6.564e+05 | 30.0 s | 19/21 |
| during | `gateway-near-limit` | ok | - | 0.85 | 0.9304 | 180.0 s | 4/21 |
| during | `gateway-restarted` | ok | - | 0 | 1 | 214.0 s | 4/4 |
| during | `frontend-5xx-blip` | ok | - | 0.02 | 0.03287 | 225.0 s | 2/21 |
| during | `order-service-unaffected` | ok | - | 0.01 | 0 | 0.0 s | 21/21 |
| after | `gateway-memory-normal` | ok | 81.3 MiB | 105.7 MiB | 79.6 MiB | 0.0 s | 7/7 |
| after | `frontend-5xx-low` | ok | - | 0.01 | 0 | 0.0 s | 7/7 |

Failures on the way (all live, all fixed before the verified runs above):
- **S2 `20261117T200402Z-658fdf` FAILED**: dropping only `ix_orders_user_id_created_at` (the plan's trigger) raised GET /orders p95 from ~8 ms to only 10–16 ms. The planner walks `ix_orders_created_at` backwards and the load generator's users have many recent orders, so it finds 20 rows almost at once (T2 measured 18–40 ms on seeded users with no recent orders). My earlier 1.6 s estimate came from `EXPLAIN ANALYZE` with all index scans disabled — not what the planner does. Fix: the fault drops both indexes the query can use (`drop-order-history-indexes`); plan §5 updated. Verified run: p95 2.4 s (histogram top bucket; true latency may be higher).
- **S5 `20261117T203649Z-e3d1dd` FAILED** recovery only (all 5 symptoms observed, OOM at ~205 s): `max(process_memory_usage_bytes)` kept reading the OOM-killed instance's last sample (201 MiB, held by Prometheus for 5 min) while the live instance was at 80 MiB. Fix: `max(max_over_time(...[30s]))` (also for the near-limit ratio).
- **S4 calibration**: a refused connection records no `http.client.request.duration` sample, so the drafted metric check could never fire; replaced by the order-service ERROR log line and `error.type=ConnectError` client spans.
- **S3 calibration**: api-gateway answers 503 (not 502) when user-service is down; frontend answers 502.
- **S5 calibration**: 20 unique URLs/s grew api-gateway 0.26 MiB/s (~13 kB per cached URL, ~8 min to the limit); raised to 45/s → 0.64–0.66 MiB/s, OOM at 205–214 s.

## Phase 2 — T9 evidence: same seed → same symptom pattern

Each scenario's verified run was repeated once with seed 42 and compared with
`python -m harness.controller compare` (tolerance: ±1 onset bucket of 15 s, ±1 log2 magnitude
bucket; trace checks contribute magnitude only). Cells are `onset/magnitude` buckets, `-` = not
applicable.

| scenario | reference run | repeat run | repeat verdict | fingerprint (ref → repeat): onset bucket / magnitude bucket per symptom | differences |
|---|---|---|---|---|---|
| `db-pool-exhaustion` | `20261117T194200Z-892d6a` | `20261117T210948Z-cbfdb8` | verified | frontend-5xx 2/3→1/2; frontend-error-traces -/3→-/3; gateway-error-traces -/3→-/3; order-5xx-share 2/1→2/1; order-error-traces -/3→-/3; order-pool-pending 1/-→1/-; order-pool-saturated 1/0→1/0; order-pool-timeout-logs 0/-→0/-; payment-unaffected 0/-→0/- | none |
| `slow-sql-missing-index` | `20261117T204818Z-99e367` | `20261117T212020Z-b68c81` | verified | account-p95-slow 1/4→1/4; orders-no-error-spike 0/-→0/-; orders-p95-slow 1/4→1/4; orders-slow-select-spans -/3→-/3 | none |
| `failed-deployment-bad-config` | `20261117T201435Z-872786` | `20261117T213058Z-58a894` | verified | bad-deploy-event 0/-→0/-; catalogue-unaffected 0/-→0/-; frontend-account-5xx 1/2→1/2; gateway-auth-5xx 3/1→3/1; gateway-auth-error-traces -/3→-/3; user-instance-churn 0/2→1/2; user-startup-failure-logs 0/-→1/- | none |
| `payment-unavailable` | `20261117T202618Z-2a9db3` | `20261117T214241Z-54338b` | verified | browsing-unaffected 0/-→0/-; checkout-5xx 3/1→2/1; order-checkout-error-traces -/3→-/3; order-payment-connect-error-spans -/3→-/3; order-payment-unavailable-logs 0/-→0/-; payment-no-traffic 4/-→4/- | none |
| `memory-leak` | `20261117T205855Z-b06df8` | `20261117T215313Z-802a22` | verified | frontend-5xx-blip 15/0→14/0; gateway-memory-growth 2/1→2/1; gateway-near-limit 12/0→12/0; gateway-restarted 14/-→13/-; order-service-unaffected 0/-→0/- | none |

The same pairs at stricter tolerances (recomputed from the records):
- magnitude ±0 (the plan's original): **S1 fails** (`frontend-5xx` bucket 3 vs 2: peaks of 40.7% vs
  39.5%, either side of the 40% bucket boundary); S2–S5 equal.
- onset ±0 and magnitude ±0: only S2 is identical; S1, S3, S5 differ in 2 symptoms and S4 in 1,
  each by one 15 s onset bucket (and S1's magnitude bucket).

## Phase 1 summary

**Implemented:** monorepo; Compose stack (postgres, redis, one-shot migrate, backend, worker,
frontend) with healthchecks; FastAPI core (settings, JSON logs with request ids, uniform errors,
metrics); schema + migration 0001; auth (argon2id, JWT access + rotating refresh with reuse
detection, rate limiting, audit); orgs/members/RBAC/projects/audit-log API with tenant isolation;
`/health`, `/ready`, `/metrics`; Next.js frontend (login/register, framed-sheet shell, projects
list/create); CI workflow; docs and ADRs 0001–0002.

**Verified at end of phase (clean `down -v` → `up --build --wait`):** all services healthy;
`/ready` 200 → 503 (redis stopped) → 200; `alembic downgrade base`/`upgrade head` on the fresh DB;
backend 160 tests passed; frontend 12 tests, lint, typecheck, build; Playwright golden path passed.

**Decisions:** ADR 0001 (JWT + rotating refresh), ADR 0002 (app-layer tenant isolation, RLS
deferred). SQLAlchemy pinned to 2.0.x per stack; uv + Python 3.12; host-side service URLs use
127.0.0.1 (WSL IPv4-only forwarding); rate limiter fails closed; readiness reports error classes
only; OpenAPI → TS types with CI drift checks; Next 16 Cache Components kept on (Suspense around
the org shell); shadcn/ui (Base UI flavour) with the CLI as a dev dependency; "framed sheet" UI
style (`docs/architecture/frontend.md`).

**Known limitations / carried forward:**
- (Resolved 2026-11-17: CI now runs on GitHub and is green on PR #1.) Originally: backend and frontend jobs were reproduced in clean Linux
  containers; the e2e job's steps were run by hand against the stack.
- No email flows: no verification, reset or invitations. Register (409) and add-member-by-email
  reveal whether an account exists; both are rate-limited.
- Rate limits key on the peer IP; behind Docker/WSL NAT all browser traffic shares one IP.
  No trusted-proxy (X-Forwarded-For) support yet.
- Access tokens stay valid ≤ 15 min after logout. Refresh reuse detection has no grace window: two
  tabs refreshing at the same instant can revoke the session (single-flight is per tab).
- No Postgres RLS (Phase 6); no expired-refresh-token cleanup job yet; project delete is a hard
  delete (switch to archive before Phase 3 adds child data).
- UI covers projects only; members, audit log and org creation exist in the API without UI.
- Base images use version tags, not digests. `/metrics` and `/docs` are unauthenticated (bound to
  127.0.0.1; `/docs` disabled when ENV=prod).
- `npm audit`: 4 moderate advisories in the shadcn CLI's toolchain (dev-only; production deps
  report 0).

## Environment notes
- **No containers on the laptop (from 2026-11-17, ADR 0004).** 8 GB host: starting the testbed drove
  Windows available memory to 554 MB in 22 s. Live checks run in a Codespace or the
  `testbed-live` workflow only (how-to: `docs/codespaces.md`).
- Docker runs inside WSL2 Ubuntu. WSL stops the VM when no WSL process is attached, which takes the
  containers down with it. Keep a WSL terminal open (or `wsl -d Ubuntu -- sleep infinity`) while
  using the stack; services have `restart: unless-stopped` and come back when the VM starts.
- WSL forwards IPv4 localhost only; use 127.0.0.1 for host-side DB/Redis URLs.
