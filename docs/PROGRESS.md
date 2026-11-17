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
| T5 Loadgen + deploy emitter | code committed — **unit-tested only; live baseline and deploy round-trip NOT yet verified** (laptop cannot run the testbed; to be verified in Codespaces/CI, ADR 0004) |
| T6 Controller + ground truth | |
| T7 S1 pool exhaustion | |
| T8 S2–S5 | |
| T9 Reproducibility + CI | partial: `testbed-live.yml` (manual live checks) and CI testbed + actionlint jobs added early (ADR 0004); scenario suite and reproducibility still to do |
| T10 Docs + report | |

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
