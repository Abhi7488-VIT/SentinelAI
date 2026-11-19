# ADR 0006 — Deterministic detection (and correlation) rules

- Status: **Accepted** (detection, T7). The correlation section is added with T8.
- Date: 2026-11-19
- Phase: 3 (Incident management)

## Context

SentinelAI must turn telemetry into detections that a human can check by hand, without a model
(CLAUDE.md invariant 8), without false positives on a quiet baseline, and without losing state
when a worker dies (invariant 5) or duplicating detections when a tick is retried (invariant 6).
The rule set, thresholds and state machine were fixed in the Phase 3 plan §6 **before** any fault
scenario was run against SentinelAI; this ADR records how they are implemented and why.

## Decision

### Pipeline (one tick every 15 s, `detection.tick`)

1. `eval_end` = now − 30 s, floored to the 15-s grid (late data must not look like a drop).
2. **Rollup** (`service_window_stats`), per active service, recomputing the last 2 min of buckets
   every tick (late spans), or up to 35 min after a gap / on first run:
   - bucket-local: requests, errors (server spans with status ERROR), p50/p95/p99;
   - **trailing 60 s ending at the bucket's end**: requests, errors, p95;
   - top 5 exception signatures (type + message with numbers/ids/hex stripped, hashed).
3. **Rules** read one rollup row (or the latest metric points / instance records) and return
   FIRE / CLEAR / NEUTRAL / NO_DATA / WARMING_UP (`app/detection/rules.py`, pure).
4. **State machine** per (service, rule) in `detector_states` (`app/detection/state_machine.py`,
   pure): ok → pending → firing → resolving → ok, plus warming_up.
5. **Detections** are inserted with `UNIQUE (org, service, rule, started_at)`; explanations are
   filled from stored numbers only (`app/detection/explain.py`).
6. `deploy_change` links, service `health_status`, then (T8) correlation — same transaction.

One transaction per tick under a Postgres advisory lock; overlapping ticks skip; a detector whose
`last_evaluated_at >= eval_end` is not evaluated again. A killed worker resumes from the rows.

### Rules (defaults; per-service overrides in `telemetry_config.detection`, validated)

| Rule | Fires | Clears | Critical (and firing ≥ 3 min) |
|---|---|---|---|
| `error_rate` | trailing-60 s error ratio ≥ 5 %, ≥ 20 requests, held 60 s | < 2 % for 120 s | ≥ 25 % |
| `latency_shift` | trailing-60 s p95 ≥ max(3 × baseline, baseline + 100 ms), ≥ 20 samples, held 120 s | < 1.5 × baseline for 120 s | ≥ 10 × baseline |
| `resource_saturation` | any pool used/max ≥ 0.9 **with pending > 0**, or memory ≥ 85 % of the *declared* limit, held 60 s | all pools < 0.7 and no waiters, memory < 70 %, for 120 s | waiters right now |
| `health_check` | no telemetry ≥ 90 s after being active in the last 10 min, or ≥ 3 new `service.instance.id`s in 5 min | telemetry back, no new instance for 2 min, held 120 s | always (heartbeat loss / crash loop) |
| `deploy_change` | a deployment on X, followed within 10 min by another detection on X or a direct neighbour of X | all linked detections resolved | — |

- **Latency baseline** = median of per-minute p95 over the 30 min before the current window. The
  per-minute p95 is the rollup's trailing-60-s p95 at minute boundaries, so no 30-minute span scan
  runs every tick. Minutes with < 20 samples or overlapping a `latency_shift` detection of the
  same service are excluded; fewer than 10 usable minutes → `warming_up`.
- **Pool metrics** follow OTel semantic conventions: `db.client.connection.count{state=used}`,
  `db.client.connection.max`, `db.client.connection.pending_requests` (per instance and pool).
- **Memory** uses `process.memory.usage` against `telemetry_config.memory_limit_bytes`; with no
  declared limit the memory half of the rule is inactive (no guessed limits).

### Why these choices

- **Trailing windows + minimum duration + minimum samples + hysteresis.** A single bad bucket can
  not fire (it needs 60–120 s of FIRE), low traffic cannot fire (NO_DATA), and a value hovering at
  the threshold produces one detection (NEUTRAL keeps it firing). NO_DATA never resolves either:
  a firing service that stops receiving traffic stays firing until there is data to judge.
- **Gaps restart timers.** If the previous evaluation is older than 60 s (worker down), pending and
  resolving timers restart instead of counting unobserved time as "held".
- **Critical needs time.** Severity starts at `warning`; it escalates only beyond a second
  threshold and after 3 min firing, so a short burst is never critical. Severity never decreases
  on an open detection.
- **Evidence is stored, not recomputed:** window, counts, thresholds and parameters, ≤ 20 sample
  trace ids (real error spans / slowest spans of the window), exception signatures, deployment id,
  linked detection ids. Phase 4's evidence store can cite these rows.

## Consequences

- Detection lag: ~30 s evaluation delay + minimum duration (60–120 s) + up to 15 s tick.
- A rollup recompute of 2 min is enough for exporters that batch ≤ 30 s; older late data is
  counted in the rollup only if the service had a gap (backfill).
- Per-service cost per tick is a handful of index-range queries (measured live: ~200 ms per tick
  for 5 services; one-off 35-min backfill ~330 ms per busy service — see
  `docs/evidence/phase-3/t7-detection-live.md`). Tens of services fit; hundreds would need the
  tick split per org/service across workers.
- Thresholds are starting points. If the T10 quiet baseline shows a detection it is reported as a
  false positive first; any change is recorded with its reason and re-run, never tuned with
  scenario ground truth in view (ADR 0003).
- Not covered: active health probes (D5: deferred to Phase 6), messaging-lag rules, per-route
  detection (rules run per service).
