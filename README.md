# SentinelAI

Autonomous incident investigation platform: ingests OpenTelemetry data, detects anomalies, groups
failures into incidents, investigates root causes with evidence-bound workflows, and proposes fixes
for human review. **It never modifies production systems or pushes code without explicit human
approval.**

> Status: **Phase 3 (Incident management)** on branch `phase-3-incident-management`: service
> registry and ingest keys, OTLP/HTTP ingestion, partitioned telemetry storage, dependency graph,
> deterministic detection, correlation into incidents, and the UI for all of it. Root-cause
> investigation is Phase 4. See [`docs/PROGRESS.md`](docs/PROGRESS.md).

## Layout

```
backend/         FastAPI API + Celery worker (Python 3.12, uv)
frontend/        Next.js 16 app (TypeScript, Tailwind, shadcn/ui, TanStack Query)
testbed/         Phase 2: instrumented sample app + fault injection
infrastructure/  Container init scripts (Postgres creates the sentinel_test database)
docs/            architecture/, decisions/ (ADRs), plans/, PROGRESS.md
.github/         CI workflow
```

## Prerequisites

| Tool | Version used | Needed for |
|---|---|---|
| Docker Engine + Compose v2 | 29.8 / v5.6 | running the stack |
| Node.js | 22 | frontend development, Playwright |
| uv | 0.13.8 (provides Python 3.12) | backend development and tests |

## Run the whole stack

```bash
cp .env.example .env
# Replace every change-me value (passwords must be URL-safe: letters, digits, - _ .), e.g.:
#   python -c "import secrets; print(secrets.token_hex(16))"      # passwords
#   python -c "import secrets; print(secrets.token_urlsafe(48))"  # JWT_SECRET

docker compose up --build -d --wait     # postgres, redis, migrate (one-shot), backend, worker,
                                        # ingest, ingest-writer, beat, frontend
docker compose ps                       # long-running services up (healthy where they have a
                                        # healthcheck); migrate Exited (0)
```

- App: <http://localhost:3000> — register, create a project, add services, create an ingest key
  (shown once) and point an OTel exporter at it. Incidents open on their own when detections fire.
- OTLP/HTTP ingest: `http://localhost:8001/api/v1/otlp` (the exporter appends `/v1/traces`,
  `/v1/logs`, `/v1/metrics`), header `Authorization: Bearer sai_…`. One key per service; data for
  another `service.name` is rejected and counted.
- API docs (OpenAPI): <http://localhost:8000/docs>
- Liveness: `curl http://localhost:8000/health` → `{"status":"ok"}`
- Readiness: `curl http://localhost:8000/ready` → 200 with per-dependency checks; 503 when Postgres
  or Redis is unreachable (try `docker compose stop redis`).
- Metrics: `curl http://localhost:8000/metrics` (Prometheus format)

Stop:

```bash
docker compose down        # stop, keep data
docker compose down -v     # stop and delete all data (fresh start)
```

Published ports bind to 127.0.0.1 only: 3000 (frontend), 8000 (API), 8001 (OTLP ingest),
5432 (Postgres), 6379 (Redis).

### Feed it the testbed (Phase 2 shop)

```bash
docker compose up -d                   # SentinelAI first: creates the shared network
make sentinel-connect REGISTER=1       # registers project `shop` + 5 services, writes keys and
                                       # SENTINELAI_OTLP_ENDPOINT to testbed/.env
make testbed-up                        # the shop; its Collector now also exports to SentinelAI
make scenario-run ID=db-pool-exhaustion SEED=42
make evaluate-incidents RUN=<run_id>   # harness-side scoring against ground truth (ADR 0003)
```

Exact steps and variables: [`docs/testbed.md` §3](docs/testbed.md#sending-telemetry-to-sentinelai-phase-3).

### Windows + WSL2 notes

- If Docker runs inside WSL2 (no Docker Desktop), run the `docker compose` commands from a WSL shell
  in the repo (`/mnt/c/...`). **Keep a WSL terminal open** while you use the stack: WSL shuts the VM
  down when no WSL process is attached, which stops the containers (they restart automatically
  when WSL starts again).
- WSL forwards IPv4 `localhost` only. Host-side URLs in `.env` therefore use `127.0.0.1`. Browser
  URLs stay `http://localhost:3000` / `:8000` (the refresh cookie needs both on the same site);
  browsers fall back to IPv4 quickly. Setting `networkingMode=mirrored` under `[wsl2]` in
  `%USERPROFILE%\.wslconfig` removes the IPv6 delay entirely.

## Develop locally

Run Postgres and Redis in Docker, the apps on the host:

```bash
docker compose up -d --wait postgres redis

# backend (terminal 1)
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:create_app --factory --reload --port 8000

# worker (terminal 2) — on Windows add --pool=solo (Celery's prefork pool is POSIX-only)
cd backend
uv run celery -A app.worker.celery_app worker --loglevel=INFO

# frontend (terminal 3)
cd frontend
npm ci
npm run dev        # http://localhost:3000
```

The backend reads `../.env` (repo root) and then `backend/.env`.

## Test

```bash
# backend — needs postgres + redis running (compose); uses the sentinel_test DB and Redis db 15
cd backend
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest -q                    # unit + integration (all)
uv run pytest -q -m "not integration"   # unit only, no services needed

# frontend
cd frontend
npm run lint
npm run typecheck
npm test
npm run build
npm run e2e        # Playwright: Phase 1 path + Phase 3 incident path; needs the full stack
```

Local benchmarks (stack running; results are for the machine they run on, written as JSON with
machine info and commit):

```bash
cd backend
DB=postgresql+asyncpg://sentinel:<password>@127.0.0.1:5432/sentinel
uv run python -m scripts.bench_incidents --database-url "$DB" --out /tmp/incidents.json
uv run python -m scripts.bench_ingest    --database-url "$DB" --out /tmp/ingest.json
```

`sentinel_test` is created by `infrastructure/postgres/init/` on the first start of an empty
Postgres volume. If your volume predates it: `docker compose down -v` (wipes data) or create it by
hand.

## Database migrations

```bash
cd backend
uv run alembic upgrade head
uv run alembic downgrade base
uv run alembic revision --autogenerate -m "describe change"   # then review the generated file
uv run alembic check                                          # models vs migrations drift
```

In Compose, the one-shot `migrate` service runs `alembic upgrade head` before the API and worker
start.

## API contract → frontend types

```bash
cd backend && uv run python -m scripts.export_openapi ../frontend/openapi.json
cd ../frontend && npm run gen:api
```

CI fails if either generated file is out of date.

## Documentation

- [Architecture overview](docs/architecture/overview.md) ·
  [Incident pipeline](docs/architecture/incident-pipeline.md) ·
  [Frontend](docs/architecture/frontend.md) · [Testbed](docs/testbed.md)
- ADRs: [0001 Authentication](docs/decisions/0001-auth.md) ·
  [0002 Tenant isolation](docs/decisions/0002-tenant-isolation.md) ·
  [0003 Testbed and evaluation integrity](docs/decisions/0003-testbed-and-evaluation-integrity.md) ·
  [0004 Live verification](docs/decisions/0004-live-verification-in-codespaces-ci.md) ·
  [0005 Telemetry storage](docs/decisions/0005-telemetry-storage.md) ·
  [0006 Detection and correlation](docs/decisions/0006-detection-and-correlation.md)
- Plans: [phase 1](docs/plans/phase-1.md) · [phase 2](docs/plans/phase-2.md) ·
  [phase 3](docs/plans/phase-3.md) · [Progress log](docs/PROGRESS.md) ·
  [Phase 3 evidence](docs/evidence/phase-3/)
