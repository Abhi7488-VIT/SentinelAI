#!/usr/bin/env bash
# Phase 4 T0: run one testbed scenario against SentinelAI as it is and record the outcome.
#   scripts/t0-run-scenario.sh SCENARIO_ID NOT_BEFORE_UTC   (e.g. slow-sql-missing-index 10:12:30)
# Waits until NOT_BEFORE_UTC and until the shop project has no open detection, snapshots the
# preconditions, runs the scenario (seed 42), scores it with the harness-side evaluator (public API
# only, ADR 0003) and exports the evidence to docs/evidence/phase-4/t0-<scenario>-<run_id>/.
# SentinelAI code is not changed by this script.
set -euo pipefail
# uv cannot reach PyPI from this Codespace today; the testbed env is already synced.
export UV_OFFLINE=1
cd "$(dirname "$0")/.."
id="$1"; not_before="$2"
work="$(mktemp -d)"

target=$(date -u -d "today $not_before" +%s)
while (( $(date -u +%s) < target )); do sleep 20; done

open_det() {
  docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -X -q -t -A -c "$1"' _ \
    "select count(*) from detections d join services s on s.id = d.service_id join projects p on p.id = s.project_id where p.slug = 'shop' and d.resolved_at is null;"
}
until [[ "$(open_det)" == "0" ]]; do echo "waiting: shop has open detections ($(date -u +%T))"; sleep 30; done

./scripts/t0-preconditions.sh > "$work/preconditions.txt" 2>&1
before=$(ls testbed/runs/"$id" 2>/dev/null | sort || true)
set +e
make scenario-run ID="$id" SEED=42 > "$work/harness-run.txt" 2>&1
harness_rc=$?
set -e
run_id=$(comm -13 <(echo "$before") <(ls testbed/runs/"$id" | sort) | tail -1)
run_id="${run_id%.json}"  # run records are <run_id>.json files
echo "harness exit $harness_rc, run $run_id"
[[ -n "$run_id" ]] || { echo "no run directory created"; cat "$work/harness-run.txt"; exit 1; }

# Let detections resolve and the incident settle before scoring (same as the Phase 3 S1 runs).
sleep 120
set +e
make evaluate-incidents RUN="$run_id" > "$work/evaluation.txt" 2>&1
eval_rc=$?
set -e

out="docs/evidence/phase-4/t0-${id}-${run_id}"
mkdir -p "$out"
(cd testbed && env -u VIRTUAL_ENV uv run python -m harness.evidence runs "$id" "$run_id" "../$out") > "$work/export.txt" 2>&1 || cat "$work/export.txt"
cp "$work"/preconditions.txt "$work"/harness-run.txt "$work"/evaluation.txt "$out"/
ev="testbed/runs/$id/$run_id/sentinelai-evaluation.json"
[[ -f "$ev" ]] && cp "$ev" "$out"/
echo "harness exit $harness_rc, evaluator exit $eval_rc, evidence in $out"
cat "$work/evaluation.txt"
