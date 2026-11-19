#!/usr/bin/env bash
# Quiet baseline: wait for WS, preconditions, 600 s detector_states sampling, then queries at WE+60s.
set -u
S=/tmp/claude-1000/-workspaces-SentinelAI/dc7f275d-79b6-4f52-8be5-4561ef85ecb8/scratchpad
OUT=$S/baseline; mkdir -p "$OUT"
WS=2026-11-19T18:48:00Z WE=2026-11-19T18:58:00Z
cd /workspaces/SentinelAI
until [ "$(date -u +%s)" -ge "$(date -u -d "$WS" +%s)" ]; do sleep 1; done
{
  echo "preconditions at $(date -u +%FT%TZ)"
  make -s scenario-status
  "$S/psql.sh" -c "SELECT state, count(*) FROM detector_states GROUP BY 1"
  "$S/psql.sh" -c "SELECT count(*) AS open_detections FROM detections WHERE resolved_at IS NULL"
  "$S/psql.sh" -c "SELECT min(bucket_start) FILTER (WHERE bucket_start > '2026-11-19 17:58:00Z') AS rollups_continuous_since FROM service_window_stats"
  docker compose ps --format '{{.Service}} {{.Image}} {{.CreatedAt}}' | grep -v testbed
} >"$OUT/preconditions.txt" 2>&1
make -s loadgen-stats >"$OUT/loadgen-before.json" 2>&1
"$S/sample_states.sh" 600 "$OUT/sampler.txt" >"$OUT/sampler-summary.txt" 2>&1
make -s loadgen-stats >"$OUT/loadgen-after.json" 2>&1
until [ "$(date -u +%s)" -ge $(( $(date -u -d "$WE" +%s) + 60 )) ]; do sleep 1; done
"$S/psql.sh" -v ws="$WS" -v we="$WE" <"$S/baseline.sql" >"$OUT/queries.txt" 2>&1
docker compose logs worker --no-log-prefix --since "$WS" 2>&1 |
  python3 "$S/ticks.py" 2026-11-19T18:47:30Z 2026-11-19T18:57:30Z >"$OUT/ticks.txt" 2>&1
docker compose ps --format '{{.Service}} {{.Status}}' >"$OUT/compose-after.txt" 2>&1
echo "done $(date -u +%FT%TZ)"
