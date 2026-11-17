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

1. **Two halves.** `testbed/app/` is the monitored system: ordinary code whose bugs ship in specific
   releases. `testbed/harness/` holds the load generator, chaos controller and ground truth.
   SentinelAI may read `testbed/app/` (Phase 5 code investigation) and its telemetry — nothing else.
2. **Faults produce genuine symptoms only.** App-level faults are releases with real bugs, deployed
   and rolled back like real releases; infrastructure faults are network toxics (Toxiproxy) on real
   links. No fault adds a marker to telemetry.
3. **Ground truth lives only in the harness** (its own `chaos` database), is written before and during
   the run, and is **verified against observed telemetry**; unverified runs are excluded from
   evaluation.
4. **Separate deployment.** The testbed is its own Compose project and network. The chaos controller
   has no Docker socket and acts only on an allow-list of testbed targets.
5. **Guards as tests.** A grep over `testbed/app/` and a content check over emitted telemetry fail
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
