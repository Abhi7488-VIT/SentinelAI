# SentinelAI

Autonomous incident investigation platform: ingests OpenTelemetry data, detects anomalies, groups
failures into incidents, investigates root causes with evidence-bound workflows, and proposes fixes
for human review. **It never modifies production systems or pushes code without explicit human
approval.**

> Status: **Phase 1 (Foundation) complete** — auth, organizations, RBAC, projects, health/readiness,
> CI. Incident features start in Phase 3. See [`docs/PROGRESS.md`](docs/PROGRESS.md).

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

docker compose up --build -d --wait     # postgres, redis, migrate (one-shot), backend, worker, frontend
docker compose ps                       # every long-running service shows (healthy); migrate Exited (0)
```

- App: <http://localhost:3000> — register, create projects.
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

Published ports bind to 127.0.0.1 only: 3000 (frontend), 8000 (API), 5432 (Postgres), 6379 (Redis).

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
npm run e2e        # Playwright golden path; needs the full stack (docker compose up)
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

- [Architecture overview](docs/architecture/overview.md) · [Frontend](docs/architecture/frontend.md)
- [ADR 0001 — Authentication](docs/decisions/0001-auth.md) ·
  [ADR 0002 — Tenant isolation](docs/decisions/0002-tenant-isolation.md)
- [Phase 1 plan](docs/plans/phase-1.md) · [Progress log](docs/PROGRESS.md)
