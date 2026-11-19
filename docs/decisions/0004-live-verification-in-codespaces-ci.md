# ADR 0004 — Live testbed verification runs in GitHub Codespaces / Actions, not on the dev laptop

- Status: **Accepted**
- Date: 2026-11-17
- Phase: 2 (Testbed)

## Context

The development laptop has 8 GB of RAM. With WSL limited to 4 GB and heavy apps closed, Windows
leaves about 2.2 GB available while WSL is stopped. Measured on 2026-11-17:

- the idle WSL VM costs about 700 MB;
- within 22 seconds of `make testbed-up` the VM grew to about 1.6 GB (Docker daemon plus file
  cache from image layers and Postgres data) and Windows' available memory fell to 554 MB, below
  the 600 MB floor agreed with the user. The memory watchdog stopped the testbed.

The testbed itself (about 0.8 GB of container memory) is not the problem; the host cannot hold the
VM plus a safety margin. Tuning (`autoMemoryReclaim=gradual`, lower WSL caps) cannot close a gap
this size.

## Decision

1. **No containers run on the laptop** for the testbed or SentinelAI from now on. Local work is
   limited to editing, unit tests, `ruff` and `mypy --strict`.
2. **All live verification runs in a GitHub Codespace (4-core, 16 GB) or in GitHub Actions**:
   - `.devcontainer/devcontainer.json` defines a Codespace with Docker-in-Docker, Python 3.12 + uv,
     Node LTS and `make`; nothing starts automatically.
   - `.github/workflows/testbed-live.yml` (manual `workflow_dispatch` only) starts the testbed,
     runs one named check (baseline, deploy round-trip, later scenarios 1–5), collects the run
     record and Prometheus/Loki/Jaeger query outputs as artifacts, and always tears down.
3. **The memory watchdog stays**, now configurable: `TB_MIN_AVAILABLE_MB` overrides the floor; on
   Linux hosts (Codespaces/CI) it reads `MemAvailable` from `/proc/meminfo` with a 1 GB default.
4. **Honest status**: work that has only passed unit tests is recorded as
   "unit-tested only; NOT yet verified live" until a Codespace or CI run proves it.

## Consequences

- A GitHub repository (remote) is required for Codespaces and Actions.
- Codespace and Actions minutes are limited on free plans; Codespaces must be stopped when idle
  and deleted when done (`docs/codespaces.md`).
- Live checks produce downloadable artifacts, which become the evidence quoted in reports.
