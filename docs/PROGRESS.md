# Progress log

Read at the start of every session. Updated at the end of each phase.

## Handoff (2026-11-17): laptop → Codespace

**State.** Branch `phase-2-testbed` is pushed to `origin`, with draft PR #1 into `main`. All 5 CI
jobs pass on GitHub (latest run `44109971914`). Phase 2 T1–T4 are done; T5 is code-complete; T6–T10
have not started.

**Verified vs not verified**
- Verified live (on the laptop, before ADR 0004): Phase 1 stack; Phase 2 T1–T4 (services, chain,
  OTel → Collector → Prometheus/Loki/Jaeger).
- Verified on GitHub: CI (backend, frontend, e2e, testbed unit tests, actionlint + shellcheck).
- **NOT verified:**
  - T5's 10-minute baseline and deploy round-trip.
  - `harness/live.py` has never run against a real stack.
  - `testbed-live.yml` has never been dispatched.
  - The devcontainer has never built successfully. Fixes `5b18a77` and `9912642` are untested.

**Hardware constraint.**
- The laptop has 8 GB RAM; WSL is capped at 4 GB.
- `make testbed-up` drove Windows free memory to 554 MB in 22 s, and Claude Code's low-memory reaper
  killed background jobs.
- So: no containers on the laptop. Live runs happen only in a 4-core / 16 GB Codespace or in
  `testbed-live.yml` (ADR 0004).

**Decisions this session (and why)**
- **Memory watchdog** (`harness/memguard.py`). The floor is set by `TB_MIN_AVAILABLE_MB`. Defaults:
  1024 MiB on Linux (`/proc/meminfo` MemAvailable) and 600 MiB on a Windows host (your limit).
  - `make testbed-up` refuses to start below the floor.
  - `memguard watch` runs `--on-breach` (testbed-down), stops the command and exits 3.
  - Why: stop the stack before the host starts swapping or killing processes.
- **Live checks write evidence files.** Every Prometheus/Loki/Jaeger query and its raw response
  goes to `testbed/runs/<check>/<run_id>/queries/`. Why: "verified" claims must quote real output
  (CLAUDE.md invariant 9).
- **Devcontainer: pinned `base:ubuntu-22.04` with docker-in-docker `"moby": false`.**
  - The `python:1-3.12-bookworm` image failed on a broken Yarn apt key.
  - The floating `base:ubuntu` tag moved to "resolute", which has no moby packages.
  - Never use floating tags here.
- **`testbed-live.yml` can only be dispatched once the file is on `main`** (GitHub rule for manual
  workflows). Until the merge, the Codespace is the only way to run live checks.
- **actionlint runs in CI only.** The laptop's permission classifier blocks running downloaded
  binaries, and the PyPI wrapper failed with an SSL error.
- **Open item:** the repository is public, and every commit's author email is a personal address.
  Hiding it means rewriting history and force-pushing. Not decided.

**Gotchas**
- **Testbed frontend is on 8081**, because 8080 is taken on the laptop. Forwarded Codespace ports:
  8081 shop, 8089 loadgen API, 9090 Prometheus, 3100 Loki, 16686 Jaeger, 3001 Grafana.
- **Use `127.0.0.1`, not `localhost`**, for host-side URLs: WSL forwards IPv4 only. The live
  runner's defaults already use 127.0.0.1.
- **WSL stops its VM, and every container in it,** when no WSL process is attached.
  `.wslconfig`: `memory=4GB`, `swap=4GB`, `processors=4`, `autoMemoryReclaim=gradual`.
- **A Codespace built from a broken config does not fix itself.** Delete it and create a new one,
  or run a full rebuild.

**Exact next steps**
1. Delete any failed Codespace, then create a 4-core one on `phase-2-testbed`
   (`docs/codespaces.md` (a)).
2. Check the creation log ends with `[post-create] finished: all steps succeeded.`, then run
   `docker version`.
3. Run `claude`, sign in, and paste the resume prompt from `docs/codespaces.md` (c).
4. In that session:
   - Run `make testbed-up`, then the 10-minute baseline, then `deploy-check`, both under memguard.
   - Quote the query outputs.
   - Fix failures and commit "T5 verified live".
5. Continue T6 → T10 with live evidence for each scenario. Then write the phase report and stop.
6. Stop the Codespace when idle; delete it when done.

| Phase | Status | Notes |
|---|---|---|
| 1. Foundation | **Complete, merged to main (2026-11-17)** | Plan: `docs/plans/phase-1.md`. |
| 2. Testbed | **In progress** (plan approved 2026-11-17) | Plan: `docs/plans/phase-2.md`; ADR 0003 accepted; live verification moved to Codespaces/CI (ADR 0004, `docs/codespaces.md`). |
| 3. Incident management | Not started | |
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
| T9 Reproducibility + CI | partial: `testbed-live.yml` (manual live checks) and CI testbed + actionlint jobs added early (ADR 0004); scenario suite and reproducibility still to do |
| T10 Docs + report | |

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
