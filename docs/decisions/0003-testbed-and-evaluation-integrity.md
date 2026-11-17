# ADR 0003 — Testbed architecture and evaluation integrity

- Status: **Proposed** (becomes Accepted on Phase 2 plan approval)
- Date: 2026-11-17
- Phase: 2 (Testbed)

## Context

SentinelAI's claims (detection precision/recall, RCA accuracy, evidence coverage — Phase 6) are only
as honest as the testbed they are measured on. If the system under test leaks the answer — a log line
saying "injecting connection leak", a span attribute `chaos=true`, a scenario file next to the code
the agent reads — every metric is inflated and the numbers are fabricated in effect (CLAUDE.md
invariant 9).

## Decision

1. **Separate the system from the answers.** `testbed/services/` is the monitored system: ordinary
   application code. `testbed/scenarios/` (ground-truth definitions) and `testbed/harness/` (load
   generator, fault controller, symptom checks, deploy emitter) hold the answers. SentinelAI may read
   `testbed/services/` (Phase 5 code investigation) and its telemetry — nothing else.
2. **Faults produce genuine symptoms only.** Triggers are things that happen to real systems: a
   traffic pattern, a schema change, a deploy with bad config, a dependency becoming unreachable
   (Toxiproxy on a real link). Deploy events are legitimate change signals and are emitted like a
   CI/CD system would. No fault adds a marker to telemetry.
3. **Ground truth lives only on the harness side** (`testbed/scenarios/<id>.json` definitions, runtime
   records under `testbed/runs/`), and a run is marked `verified` only when every expected symptom was
   **observed in the telemetry** and the baseline returned; unverified runs are excluded from
   evaluation.
4. **Separate deployment.** The testbed is its own Compose project and network. The chaos controller
   has no Docker socket and acts only on an allow-list of testbed targets.
5. **Guards as tests.** A grep over `testbed/services/` and a content check over emitted telemetry fail
   the build if tell-tale words (fault, chaos, inject, scenario, ground truth) appear.

## Alternatives considered

- **Feature flags named after faults inside the app** — simplest, but the flag names and their
  evaluation would appear in code and possibly telemetry. Rejected.
- **Docker-level chaos (stop/kill containers via the socket)** — realistic for crashes, but gives
  the harness root-equivalent access to the host's Docker. Rejected for Phase 2; crash scenarios can
  use the restart path that releases already use.
- **Ground truth inside SentinelAI's database** — convenient for evaluation joins, but puts the
  answers one query away from the system being evaluated. Rejected; Phase 6 imports them after runs.

## Consequences

- Phase 6's harness must join ground truth to SentinelAI output by time window and services, not by
  shared ids.
- Adding a scenario means adding a real bug (in a release) or a real network fault — more work than
  a flag, by design.
