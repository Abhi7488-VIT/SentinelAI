# Progress log

Read at the start of every session. Updated at the end of each phase.

| Phase | Status | Notes |
|---|---|---|
| 1. Foundation | In progress (started 2026-11-17) | Plan: `docs/plans/phase-1.md` (approved 2026-11-17) |
| 2. Testbed | Not started | |
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
| T7 Readiness | |
| T8 Frontend | |
| T9 CI | |
| T10 Docs + report | |

## Environment notes
- Docker runs inside WSL2 Ubuntu. WSL stops the VM when no WSL process is attached, which takes the
  containers down with it. Keep a WSL terminal open (or `wsl -d Ubuntu -- sleep infinity`) while
  using the stack; services have `restart: unless-stopped` and come back when the VM starts.
