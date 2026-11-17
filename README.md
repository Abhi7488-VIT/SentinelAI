# SentinelAI

Autonomous incident investigation platform: ingests OpenTelemetry data, detects anomalies, groups failures
into incidents, investigates root causes with evidence-bound workflows, and proposes fixes for human review.
It never modifies production systems or pushes code without explicit human approval.

> Status: Phase 1 (Foundation) in progress. See `docs/PROGRESS.md`.
> Run instructions are completed at the end of Phase 1.

## Layout

```
backend/         FastAPI app + Celery worker (Python 3.12)
frontend/        Next.js (App Router) UI
testbed/         Phase 2: instrumented sample app + fault injection
infrastructure/  Container init scripts and infra config
docs/            architecture, ADRs (decisions/), phase plans, PROGRESS.md
.github/         CI workflows
```
