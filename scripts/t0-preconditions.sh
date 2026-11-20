#!/usr/bin/env bash
# Phase 4 T0: precondition snapshot before a scenario run (same checks as the Phase 3 S1 run).
# Read-only. Prints: active scenario, detector states, open detections, rollup coverage of the
# last 35 min (all scoped to the testbed's `shop` project), loadgen totals, repo HEAD. Postgres credentials are read inside the container.
set -euo pipefail
cd "$(dirname "$0")/.."

psql() {
  docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -X -q "$@"' _ "$@"
}

echo "== preconditions at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
./testbed/tb.sh scenario status
SHOP="select s.id from services s join projects p on p.id = s.project_id where p.slug = 'shop'"
SHOP_PROJECT="select id from projects where slug = 'shop'"
psql -c "select state, count(*) from detector_states where service_id in ($SHOP) group by state order by state;"
psql -c "select count(*) as open_detections from detections where resolved_at is null and service_id in ($SHOP);"
psql -c "select number, status, title, last_activity_at from incidents where project_id in ($SHOP_PROJECT) order by number;"
psql -c "select min(bucket_start), max(bucket_start), count(distinct bucket_start) as buckets
         from service_window_stats where bucket_start > now() - interval '35 minutes' and service_id in ($SHOP);"
curl -fsS http://127.0.0.1:8089/stats; echo
git rev-parse HEAD
