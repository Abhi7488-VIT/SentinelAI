# Testbed

A five-service shop instrumented with OpenTelemetry, a seeded load generator, and a fault
controller with five reproducible incidents and ground-truth records. SentinelAI is evaluated
against it. Full documentation: [`docs/testbed.md`](../docs/testbed.md).

```bash
make testbed-up                       # from the repo root; waits until healthy
make scenario-list
make scenario-run ID=db-pool-exhaustion   # ~10 min: baseline, fault, checks, recovery, verdict
make testbed-down                     # CLEAN=1 also deletes the data
```

Needs Docker and about 3 GB of free memory; the development laptop cannot run it (ADR 0004), so
live runs happen in a Codespace (`docs/codespaces.md`) or the `testbed-live` /
`testbed-scenarios` GitHub Actions workflows.

| Directory | Contents |
|---|---|
| `services/` | the monitored system (the only part SentinelAI may read) |
| `harness/` | load generator, deploy pipeline, fault controller, live checks |
| `scenarios/` | ground-truth definitions and their JSON schema |
| `infra/` | Collector, Prometheus, Loki, Jaeger, Grafana, Toxiproxy config |
| `runs/` | run records and query outputs (gitignored) |
