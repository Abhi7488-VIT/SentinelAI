# Progress log

Read at the start of every session. Updated at the end of each phase.

## Phase 3 — complete on branch `phase-3-incident-management`, awaiting review (not merged)

Plan: `docs/plans/phase-3.md`, approved 2026-11-19 (D1–D6 as proposed). Work runs in the Codespace
(4 cores, 15 GiB); SentinelAI's Postgres/Redis run next to the testbed.

| Task | Status |
|---|---|
| T1 Registry | done — see below |
| T2 Telemetry storage | done — see below |
| T3 OTLP endpoint | done — see below |
| T4 Ingest writer | done — see below |
| T5 Testbed wiring (live) | done — see below |
| T6 Dependency graph | done — see below |
| T7 Detection | done — see below |
| T8 Correlation + incidents | done — see below (grouping verified in tests; live S1 is T10) |
| T9 UI | done — see below (backend APIs, 5 pages, Vitest, Playwright incident path green on the live stack) |
| T10 Live acceptance + benchmarks | done — quiet baseline ×2 clean; **S1 PASS** (`20261119T211437Z-1eaa97`); benchmarks in `docs/evidence/phase-3/bench/` |
| T11 Docs + report | done — README, `docs/architecture/incident-pipeline.md`, overview/frontend docs, this report |

## Phase 3 report (2026-11-20) — incident management

All work ran in the Codespace (4 cores, 15.6 GiB). Branch `phase-3-incident-management`, 29 commits
ahead of `main` (incl. this report), pushed; **not merged, no PR opened**. Phase 4 not started.

### Implemented
- **T1** service registry, per-service ingest keys (shown once, SHA-256 at rest), project/service
  archive. **T2** day-partitioned telemetry storage, retention, `beat`. **T3** separate OTLP/HTTP
  ingest process with size/rate/record limits, partial success, bounded queue. **T4** idempotent
  writer with retries and dead-letter. **T5** testbed Collector → SentinelAI, live.
  **T6** dependency graph (declared + observed, evidence-bound). **T7** deterministic detection
  (5 rules, state machine, templated explanations). **T8** correlation into incidents with
  structural reasons, incident APIs and timeline.
- **T9** overview and ingest-usage APIs; UI: project Overview (Recharts), Services + keys, Service
  map (React Flow, own deterministic layout), Incidents list (URL filters) and detail (transitions,
  explanations, grouping reasons, timeline); Playwright incident golden path.
- **T10** quiet baseline ×2, S1 live acceptance with a harness-side evaluator, local benchmarks.
- **T11** docs (below) and this report.

### Acceptance criteria (plan §12) — what was run
| Criterion | Result | Proof |
|---|---|---|
| S1 → one incident, correct services, correlated detections, readable explanation | **PASS** on run `20261119T211437Z-1eaa97`: 1 incident, api-gateway/frontend/order-service, payment- and user-service excluded, 7 detections, 6 joiners each with ≥ 1 structural reason, 0 false positives, first detection 68 s / incident 112.7 s after fault start. Earlier runs: `e076ed` FAIL (SentinelAI timestamp bug, fixed `0a9b73b`), `335bba` FAIL under the original evaluator (evaluator opener bug, fixed `afcafd7`; re-score PASS, original output kept) | `docs/evidence/phase-3/s1-*/` |
| Quiet baseline 10 min → 0 incidents | **PASS ×2**: 0 detections, 0 incidents, no rule entered `pending` (before and after the `0a9b73b` fix) | `t10-quiet-baseline.md`, `t10-baseline-20261119T1848Z/` |
| Duplicate batch → no duplicates | **PASS**: integration tests (same request twice → 10 rows not 20; redelivered batch no-op) + live re-POST (T4); benchmark: rows = accepted at every level | `t4-writer-live.md`, `bench/` |
| Malformed → 4xx and counted | **PASS**: tests + live: 400 `malformed_payload`, partial success rejected 3/10, both in `ingest_records_total` and `GET …/ingest-usage` | `bench/README.md` |
| Overload → 429, not OOM | **PASS**: live flood 6,091 requests → 429/503 only, ingest peak 126.7 of 384 MiB, no restart (T3); benchmark peak 128.6 MiB | `t3-overload.md`, `bench/` |
| Cross-org isolation on every new endpoint | **PASS**: OpenAPI-driven guard calls all **31** org-scoped operations as a non-member → 404; explicit foreign-object-id tests for services, keys, detections, incidents (detail/events/transitions), graph, overview, ingest-usage; foreign ids as list filters return nothing (mutation-checked) | `test_tenant_isolation.py`, `test_correlation.py`, `test_overview.py`, `test_detection.py`, `test_service_graph.py` |
| Measured numbers | `GET /incidents` on 50k: p95 20–32 ms per filter (no-match title search p95 93.6 ms); ingest 1,896–2,370 records/s until written, writer-bound | `bench/README.md` (labelled: this Codespace, 2026-11-20, `701056e`) |

### Tests (run at the final commit of the code, this machine)
- Backend: ruff format + check clean, mypy strict clean (172 files), `alembic check` clean,
  **409 passed** (160 at the start of Phase 3).
- Frontend: lint, typecheck, **33 Vitest tests** (8 files), `next build` pass; OpenAPI types in sync.
- Playwright (live stack): **2 passed** (Phase 1 path + Phase 3 incident path), green on 4 runs.
- Testbed/harness: **145 passed**, 5 deselected (live-stack tests; covered by the runs above).

### Not verified / known limitations
- **CI has not run any of Phase 3 on GitHub**: `ci.yml` runs on pull requests and pushes to `main`
  only, and no PR is open. The new e2e spec needs `ingest`, `ingest-writer` and `beat` up in the CI
  compose stack; they are in `docker-compose.yml`, but this is untested on a runner.
- Only scenario S1 was run against SentinelAI (plan scope: golden path first). S2–S5 are not
  evaluated; detection of slow SQL, failed deploys, an unreachable dependency and a memory leak is
  untested live.
- One S1 run at one seed passes: a working golden path, not a detection rate. One baseline window
  at one traffic mix: no false positive seen, not a false-positive rate.
- The writer (~2k records/s here) is the ingest ceiling; one sender fills the queue.
- Observed `redis:redis` vs declared `redis:sessions` (Redis spans carry no `db.namespace`).
- Correlation and detection run in one transaction per tick for all orgs (fine at this scale).
- A user resolving an incident in the same instant a tick links a detection is not serialized.
- 10-active-keys limit is a soft limit under concurrent creates; no un-archive endpoint.
- UI: light theme only, Overview caps at 50 services, map layout without crossing minimisation,
  polling (SSE is Phase 4), no trace viewer for sample trace ids.
- Worker processes expose no Prometheus endpoint (structured logs only).
- Ingest has no per-IP limit on failed auth (port bound to 127.0.0.1 / compose network).
- Slow no-match title search (p95 93.6 ms on 50k) is not analysed with `EXPLAIN` yet.

### Decisions (and why); plan deviations
- D1–D6 as approved. Deviations, each recorded in its task section: maintenance CLI in `app/cli/`
  (Docker image excludes `scripts/`); ingest queue bound as max pending batches + max batch size
  instead of a byte counter (cannot drift if a writer dies); `deploy_proximity` uses deploy_change
  links, not "any incident service" (would merge on timing alone); graph node counts and
  `GET /overview` moved to T9; overload measured with `scripts/ingest_overload.py` (T3) rather than
  `bench_ingest.py`, which measures sustained throughput instead.
- `jit=off` on SentinelAI's connections (JIT made the graph query 733 ms vs 16.7 ms).
- Detection rules evaluate everything as of `eval_end` (fix after S1 `e076ed`); no threshold was
  changed at any point in Phase 3.
- The harness evaluator identifies an incident's opener from its `created` event (fix after
  `335bba`); pass rule unchanged, original FAIL output kept.
- UI dependencies `recharts` 3.10.1, `@xyflow/react` 12.12.1, pinned exact, chosen over releases
  younger than two weeks. React Flow attribution kept.
- Removed Phase 1's static "No telemetry yet" project label (false once ingestion exists).
- Chart series stop at the last complete minute (rollups run 30 s behind; avoids a false drop).

### Files changed this phase (summary)
`git diff --shortstat origin/main...HEAD`: 261 files, +46,411 / −1,414 (≈ half is committed
evidence). Backend: `app/{api,services,repositories,models,schemas,detection,ingest,cli}`,
migrations `0002`–`0008`, `scripts/{connect_testbed,ingest_overload,bench_incidents,bench_ingest}.py`,
tests. Frontend: `src/components/{common,project,services,incidents,overview,service-map}`, new
routes under `src/app/(app)/orgs/[orgId]/`, `e2e/incident-path.spec.ts`. Compose: `ingest`,
`ingest-writer`, `beat`. Testbed: SentinelAI overlay, registry file, `harness.evaluate`. Docs:
README, `docs/architecture/{overview,incident-pipeline,frontend}.md`, ADRs 0005–0006,
`docs/evidence/phase-3/`, this file.

### Remaining / next
- Your review; then a PR to `main` so CI (including the new e2e job) runs on GitHub for the first
  time — fix anything it finds before merging.
- Open item carried from Phase 2: commit author emails are personal; hiding them needs a history
  rewrite. Not decided.
- Candidates for later (not started): S2–S5 against SentinelAI, `EXPLAIN` of the no-match search,
  writer throughput if real load needs it.
- **Stop here; Phase 4 waits for your go-ahead.**

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

### T6 Dependency graph (2026-11-19, live)
- Migration `0006`: `service_edges` (declared/observed; DB CHECK `evidence_required`: declared ⇒
  cites its `service_dependencies` row via composite FK with cascade, observed ⇒ 16-byte sample
  trace id; composite tenant FKs on both ends), backfill of declared edges from existing
  dependencies, new partial index `spans (org_id, service_id, start_time) WHERE kind = 3 AND
  db_system IS NOT NULL`.
- Declared edges are written in the same transaction as `PUT …/dependencies`. Observed edges:
  beat task `graph.discover_edges` every 60 s over the last 2 min, per active service: callers
  from parent/child spans across services (same project, active only), DB externals named
  `<db.system>:<db.namespace or server.address>` — validated against the external-name pattern,
  max 50 per service; observed edges expire after 7 days unseen. Advisory lock, one transaction,
  upserts → repeat runs change nothing.
- `GET /api/v1/orgs/{org_id}/service-graph?project_id=` (viewer): service + external nodes, both
  edge sources, `truncated` flag (hard cap 5,000 edges; registry-bounded, not paginated).
- Verified: **326 backend tests pass** (+13: evidence CHECKs, cross-tenant FK, declared sync on
  replace/clear, discovery of service + DB edges with sample traces, repeat-run idempotency,
  window/same-service negatives, cross-project + archived callers skipped, invalid/over-cap external
  names skipped, expiry vs declared, lock skip, API nodes/edges, archived hidden, foreign project
  404, 0006 backfill); isolation guard now also sends `project_id`; ruff + mypy strict clean;
  `alembic check` clean; OpenAPI + TS types regenerated, frontend typecheck/lint pass.
  **Live** (`docs/evidence/phase-3/t6-graph-live.md`): first scheduled run 146 ms, 5 services,
  4 service + 4 external edges, 0 skipped; observed graph = frontend → api-gateway → {user, order},
  order → payment, + postgresql:{orders,payments,users} and redis; a sample trace checked span by
  span. EXPLAIN on live data: both queries use the partial window index + PK lookup, 2–7 ms.
- Fix on the way: the backend tests read the shell's exported `.env` (retention days, and
  Celery's `CELERY_BROKER_URL` override sent a test's tasks to the live broker); tests now drop
  env vars that map to Settings fields (separate commit).
- Plan deviation: graph nodes carry health + last_seen only; the last-15-min request/error counts
  come from `service_window_stats`, which T7 creates — added there.
- Limitations: observed `redis:redis` ≠ declared `redis:sessions` (Redis spans have no
  `db.namespace`); `call_count` = calls in the latest 2-min window; messaging edges and HTTP calls
  to uninstrumented hosts are not discovered; worker processes still expose no Prometheus
  endpoint (job emits a structured `graph_discovery_done` log with counts and duration).

### T7 Detection (2026-11-19, live pipeline; no fault injected yet)
- Migration `0007`: `service_window_stats` (15-s buckets: bucket-local requests/errors/p50/p95/p99
  + trailing-60-s requests/errors/p95 + top-5 exception signatures), `service_instances`,
  `detector_states`, `detections` (`UNIQUE (org, service, rule, started_at)`). ADR 0006 written.
- Pure logic in `app/detection/`: rules (error_rate, latency_shift, resource_saturation,
  health_check; deploy_change in the engine), state machine (min duration, hysteresis, NO_DATA
  holds, warming_up, gap restarts timers), templated explanations, exception signatures.
- `detection.tick` (beat, 15 s): eval at now − 30 s on the 15-s grid; advisory lock, one
  transaction; per-detector idempotency on `last_evaluated_at`. Severity: warning → critical only
  beyond the critical threshold and ≥ 3 min firing. Services' `health_status` set here only.
  Per-service overrides in `telemetry_config.detection` (validated: clear < fire ≤ critical).
  Rollups/instances: 30-day retention in the maintenance job.
- API: `GET /detections` (service, rule, open/resolved, from/to, cursor), `GET /detections/{id}`,
  `GET /detector-states`.
- Verified: **375 backend tests pass** (+43 unit: per-rule tables, short spike, flapping → one
  detection, clear-hold, NO_DATA, warming_up, restart mid-pending, gap, explanation snapshot,
  signatures, override validation; +6 integration over simulated time on Postgres: error spike
  fires once at the expected times, escalates to critical, resolves after 120 s clear, links a
  deploy, retried tick is a no-op, evidence trace ids are real error spans; short spike never
  fires; latency warming_up then fires vs median baseline; saturated pool with waiters; crash
  loop + heartbeat loss → critical, service `unhealthy`; API filters + cross-org 404).
  ruff/mypy strict clean; `alembic check` clean.
  **Live** (`docs/evidence/phase-3/t7-detection-live.md`): ticks every 15 s, first (35-min
  backfill) 2.16 s, then ~190–310 ms for 5 services; all 20 detectors evaluating; 0 detections in
  the first minutes of baseline (not the T10 quiet-baseline measurement). EXPLAIN: rollup uses
  the server-span window index (8 ms per service; 331 ms one-off backfill), exceptions the error
  index.
- Bug found by the tests: the error-rate "baseline" window ended at the firing window, so it
  included the anomaly's first seconds; it now ends where the first anomalous window began.
- Plan deviations: graph node request/error counts come from `service_window_stats` (API wiring in
  T9 with the Overview); per-minute p95 = trailing-60-s p95 at minute boundaries (same statistic,
  no 30-min span scan per tick).
- Limitations: one transaction per tick for all orgs (fine for one org / tens of services);
  pool metrics only in OTel semconv names; worker metrics still logs only.

### T8 Correlation + incidents (2026-11-19)
- Migration `0008`: `incidents` (per-org number, derived title/severity/start, status CHECKs,
  keyset/status/started_at indexes, `pg_trgm` GIN on title, partial index for active ones),
  `incident_detections` (detection id unique → linked once; reasons + score), `incident_services`,
  `incident_events` (BIGINT identity ids for Phase 4 SSE replay).
- Correlator runs at the end of every detection tick (same transaction/lock). Reasons:
  shared_trace 3, exception_signature 3, dependency_edge 2, deploy_proximity 2, same_service 1;
  at least one required (time alone never merges); highest score, ties → oldest; otherwise a new
  incident. Severity follows detections (severity_changed events); `all_detections_resolved`
  after 10 quiet minutes; the system never resolves incidents.
- API: `GET /incidents` (project, status, severity, service, from/to, `q` = title or `#number`,
  cursor), `GET /incidents/{id}` (services, detections with explanations + reasons),
  `POST /incidents/{id}/transitions` (engineer+, audited, 409 `invalid_transition`),
  `GET /incidents/{id}/events?after_id`.
- Verified: **397 backend tests pass** (+8 integration: cascading failure in shared traces →
  one incident with frontend/api-gateway/order-service and not payment-service, joins carry
  shared_trace + dependency_edge, retried tick and a second correlator pass add nothing;
  time-only proximity → two incidents; each of dependency_edge / exception_signature /
  shared_trace merges alone; tie → older incident; resolved incident takes nothing (new incident);
  all_detections_resolved after 10 min and status stays open; API filters/search incl. literal
  wildcards, detail, viewer 403, invalid 409, full transition chain + events paging + audit,
  cross-org 404 on every incident route; +14 unit: titles, severity, transition table).
  ruff/mypy strict clean; `alembic check` clean. EXPLAIN on 50k incidents: every filter
  0.06–2.4 ms with the intended indexes (`docs/evidence/phase-3/t8-correlation.md`).
  **Live:** migration applied, ticks with correlation ~260–300 ms, APIs answer; 0 detections at
  baseline so correlation itself is **not yet verified live** (S1 run, T10).
- Plan deviation: `deploy_proximity` uses deploy_change links (deployment on the detection's
  service or a direct neighbour) instead of "any incident service", which would merge on timing
  alone (ADR 0006). `GET /overview` moves to T9 with the Overview page.
- Found while verifying: detections firing in the same tick were correlated in random-UUID
  order (flaky test: which join carried `dependency_edge` varied). Now ordered by
  (fired_at, started_at, service_id, rule); test asserts order-independent facts; 3 repeat runs
  green.
- Limitations: a user resolving an incident in the same instant a tick links a detection to it
  is not serialized.

### T10 (part 1) Quiet baseline (2026-11-19, live)
- 10 min, 16:54:52Z → 17:04:56Z, no scenario active, loadgen `baseline`, after > 60 min of
  rollups (all latency baselines had 30 usable minutes; nothing `warming_up`).
- **0 incidents, 0 detections; 0 of 2,360 detector samples (every 5 s) were anything but
  `ok`/`clear`** — no rule entered `pending`. 40 ticks (none skipped, no errors), p50 230 ms,
  p95 384 ms, max 526 ms. Headroom: error ratio 0 (fire 5 %); worst p95 0.34× the latency fire
  threshold; pools ≤ 1/10 used, 0 waiters; 0 new instances. Loadgen: 6,015 requests, 0 errors.
- Evidence: `docs/evidence/phase-3/t10-quiet-baseline.md` (method, raw output). One window at one
  traffic mix: no false positive here, not a false-positive rate. Thresholds unchanged.

### T10 (part 2) S1 db-pool-exhaustion, run `20261119T171535Z-e076ed` (2026-11-19, live) — FAIL
- New harness-side evaluator `harness.evaluate incidents` (`172a834`, before the run): reads the
  run record + SentinelAI's public API only; 17 unit tests; `make evaluate-incidents RUN=…`.
- Harness: VERIFIED (fault 17:17:35Z → 17:22:36Z, all symptoms, payment unaffected).
- SentinelAI: **1 incident** ("Resource saturation, latency and errors in order-service,
  api-gateway, frontend"), 7 detections, **exactly the expected services**, payment-service and
  user-service excluded, every join structural (shared_trace on all 6), all explanations pass the
  readability checks, incident created 72.8 s after fault start.
- **Evaluator: FAIL.** The first detection (order-service `resource_saturation`) is stamped
  `started_at` 17:17:15Z, 20 s before the fault; the first saturated metric point is 17:17:43.96Z.
  Cause: metric (and health) rules read data up to `now` but are stamped on `eval_end`
  (now − 30 s). This puts the incident start outside the fault window and counts 1 false positive.
  Fix proposed (evaluate all rules as of `eval_end`, tests that fail on current code, re-run
  baseline + S1); **not applied**. Evidence: `docs/evidence/phase-3/s1-20261119T171535Z-e076ed/`.

### T10 (part 3) Fix, baseline re-run, S1 re-runs (2026-11-19, live)
- Fix `0a9b73b`: saturation and health rules evaluated as of `eval_end` (tests that failed on the
  old code). Quiet baseline re-run 18:48–18:58Z on `0a9b73b`
  (`docs/evidence/phase-3/t10-baseline-20261119T1848Z/`): **0 detections, 0 incidents**, 40 ticks.
- S1 run `20261119T185934Z-335bba`: one incident, the 3 expected services, 0 false positives, but
  **FAIL** on `grouping_is_structural`. Traced (`87d5c41`) to the evaluator, not SentinelAI: it
  exempted `detections[0]` as the opener, but the API sorts by `(started_at, id)` and all 7 started
  in the same bucket. Evaluator fix `afcafd7`: the opener is the detection named by the incident's
  `created` event (pass rule unchanged). The `335bba` re-score is PASS; the original FAIL output is
  kept next to it.
- **S1 run `20261119T211437Z-1eaa97` (fresh, after 30 min warm-up, evaluator `afcafd7`): PASS.**
  Harness VERIFIED (fault 21:16:37Z → 21:21:37Z). Incident #3 "Latency, errors and resource
  saturation in api-gateway, frontend, order-service", critical; 7 detections, every joiner with
  ≥ 1 reason; payment-service and user-service excluded; 0 false positives; first detection 68 s
  and incident 112.7 s after fault start. One run at one seed: shows the golden path works on this
  run, not a detection rate. Evidence: `docs/evidence/phase-3/s1-20261119T211437Z-1eaa97/`.

### T10 (part 4) Benchmarks (2026-11-20)
- See the Phase 3 report above and `docs/evidence/phase-3/bench/README.md` (incl. the failed
  script runs before the recorded ones).

### T9 UI (2026-11-19/20)
- Backend (`856642d`): `GET /overview?project_id` (open incidents by severity, newest 5,
  detections in 24 h by rule, per active service health / last seen / ingest accepted+rejected in
  15 min / requests+errors per minute over 60 min from the rollups) and
  `GET /services/{id}/ingest-usage` (per-minute accepted/rejected by signal and reason, summed over
  keys, keyset-paged, default 1 h, max 24 h → 422 `invalid_time_range`). 8 integration tests;
  EXPLAIN on live data (`docs/evidence/phase-3/t9-overview-explain.txt`): with seqscan off every
  query uses an org-prefixed index; tables are small, so seq scans are chosen by default.
- Isolation (`303ec77`): Org A's service/project ids passed as **filters** to Org B's incidents,
  detections and detector-states lists return an empty page (same as an unknown id); the graph of
  Org A's project is 404. Mutation-checked (removing the org filter in `TenantRepository` makes it
  fail). The OpenAPI-driven guard now calls **31** org-scoped operations as a non-member, all 404
  (the `856642d` commit message says 29; 31 is the count from `openapi.json`).
- UI (`9dcc98e`, `4256648`, `ee79055`, `53eee63`): project frame with Overview / Services / Service
  map tabs; Services + ingest keys (key shown once); Incidents list with URL-synced filters and
  detail with transitions, explanations, grouping reasons, timeline; Overview with Recharts;
  service map with React Flow over a deterministic layout written here. Dependencies: `recharts`
  3.10.1 and `@xyflow/react` 12.12.1 (plan §10), exact pins, chosen over releases < 2 weeks old;
  `npm audit --omit=dev` 0 vulnerabilities.
- Fixed on the way: the Projects list still said "No telemetry yet" for every project (Phase 1's
  honest placeholder, false since ingestion exists) — removed; status now only comes from the API.
- Found by looking at the pages over the testbed's real data (`701056e`): every chart ended in a
  false drop (the newest minute is still filling; now not plotted), the overview was 537 px wide at
  a 390 px viewport (fixed; all six pages measured scrollWidth 390), edge table order.
- E2E (`701056e`): `e2e/incident-path.spec.ts` on the live stack — project → service → key →
  3 min of backdated failing spans through the public ingest API → incident "Errors in
  checkout-api" appears by itself → detail (opener, timeline) → "Start investigating" recorded →
  overview and map show the service. **2/2 Playwright tests green on 4 runs here** (incident path
  1.3–1.4 min); a probe script measured the incident 75.3 s after the POST. The Phase 1 spec was
  updated for the removed placeholder text. **CI's e2e job runs on pull requests only: not run on
  GitHub yet.**
- Checks at `701056e`: frontend typecheck, lint, **33 Vitest tests**, `next build` pass; backend
  ruff + mypy strict clean, **409 tests pass**.
- Limitations: no dark theme; the Overview caps at 50 services (`services_truncated`); the map's
  layout is plain layering (no crossing minimisation); polling every 15 s until SSE (Phase 4);
  incident detail does not link sample trace ids to a trace viewer (none in SentinelAI yet).

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
| 3. Incident management | **Complete on branch, awaiting review** (2026-11-20) | Plan: `docs/plans/phase-3.md` (approved 2026-11-19); report below. |
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
