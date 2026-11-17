# Progress log

Read at the start of every session. Updated at the end of each phase.

| Phase | Status | Notes |
|---|---|---|
| 1. Foundation | **Complete, merged to main (2026-11-17)** | Plan: `docs/plans/phase-1.md`. |
| 2. Testbed | **In progress** (plan approved 2026-11-17) | Plan: `docs/plans/phase-2.md`; ADR 0003 accepted. |
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
| T9 CI | done (validated locally; no GitHub remote yet) |
| T10 Docs + report | done |

## Phase 2 task log

| Task | Status |
|---|---|
| T1 Skeleton (uv project, compose, limits, Makefile + tb.sh) | done |
| T2 Services | done |
| T3 Telemetry + Collector | done |
| T4 Backends | |
| T5 Loadgen + deploy emitter | |
| T6 Controller + ground truth | |
| T7 S1 pool exhaustion | |
| T8 S2–S5 | |
| T9 Reproducibility + CI | |
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
- CI has never run on GitHub (no remote). Backend and frontend jobs were reproduced in clean Linux
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
- Docker runs inside WSL2 Ubuntu. WSL stops the VM when no WSL process is attached, which takes the
  containers down with it. Keep a WSL terminal open (or `wsl -d Ubuntu -- sleep infinity`) while
  using the stack; services have `restart: unless-stopped` and come back when the VM starts.
- WSL forwards IPv4 localhost only; use 127.0.0.1 for host-side DB/Redis URLs.
