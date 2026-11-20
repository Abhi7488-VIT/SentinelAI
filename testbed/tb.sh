#!/usr/bin/env bash
# SentinelAI testbed workflow. The repo-root Makefile calls this; it also works on its own:
#   testbed/tb.sh up | ui | down [--clean] | status | logs [service] | test
#   testbed/tb.sh deploy SERVICE VERSION [KEY=VALUE ...] | loadgen-stats | loadgen-mix NAME
#   testbed/tb.sh scenario list | validate | status | run ID [opts] | start ID | stop ID
set -euo pipefail

TB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose -f "$TB_DIR/docker-compose.yml" --env-file "$TB_DIR/.env")
# Connected to SentinelAI (make sentinel-connect)? Then the Collector also joins its ingest network.
if [[ -f "$TB_DIR/.env" ]] && grep -Eq '^SENTINELAI_OTLP_ENDPOINT=.+' "$TB_DIR/.env"; then
  COMPOSE+=(-f "$TB_DIR/docker-compose.sentinelai.yml")
fi

log() { printf '[testbed] %s\n' "$*"; }
die() { printf '[testbed] error: %s\n' "$*" >&2; exit 1; }

random_secret() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 16
  else head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}

ensure_env() {
  if [[ -f "$TB_DIR/.env" ]]; then add_missing_env_keys; return; fi
  log "creating testbed/.env from .env.example with random secrets"
  local line key value
  : > "$TB_DIR/.env"
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ ^([A-Z_][A-Z0-9_]*)=(change-me.*)$ ]]; then
      key="${BASH_REMATCH[1]}"
      value="$(random_secret)"
      printf '%s=%s\n' "$key" "$value" >> "$TB_DIR/.env"
    else
      printf '%s\n' "$line" >> "$TB_DIR/.env"
    fi
  done < "$TB_DIR/.env.example"
}

add_missing_env_keys() {
  # Keys added to .env.example after .env was created get a value (random for secrets).
  local line key
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^([A-Z_][A-Z0-9_]*)=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}"
    grep -q "^${key}=" "$TB_DIR/.env" && continue
    if [[ "${BASH_REMATCH[2]}" == change-me* ]]; then
      printf '%s=%s\n' "$key" "$(random_secret)" >> "$TB_DIR/.env"
    else
      printf '%s\n' "$line" >> "$TB_DIR/.env"
    fi
    log "added missing key $key to testbed/.env"
  done < "$TB_DIR/.env.example"
}

ensure_runs_dir() {
  # Bind-mounted into the controller and deployer (which run as a non-root user).
  mkdir -p "$TB_DIR/runs"
  chmod a+rwx "$TB_DIR/runs" 2>/dev/null || true
}

warn_if_sentinelai_running() {
  if [[ -n "$(docker ps -q --filter label=com.docker.compose.project=sentinelai 2>/dev/null)" ]]; then
    log "warning: the SentinelAI stack is running; on a 4 GB WSL budget stop it first (docker compose down in the repo root)"
  fi
  if [[ -r /proc/meminfo ]]; then
    local avail
    avail=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo)
    log "memory available: ${avail} MiB"
  fi
}

memguard_check() {
  # Refuse to start below the memory floor (TB_MIN_AVAILABLE_MB; default 1024 MiB on Linux).
  command -v python3 >/dev/null 2>&1 || die "python3 is required for the memory check"
  PYTHONPATH="$TB_DIR" python3 -m harness.memguard check \
    || die "not enough free memory to start the testbed (set TB_MIN_AVAILABLE_MB to change the floor)"
}

cmd_up() {
  ensure_env
  ensure_runs_dir
  memguard_check
  warn_if_sentinelai_running
  log "building and starting (waits until every service is healthy)"
  "${COMPOSE[@]}" up --build -d --wait --wait-timeout 300
  cmd_status
}

cmd_ui() {
  log "starting Grafana (profile ui) on http://127.0.0.1:3001 (anonymous viewer)"
  "${COMPOSE[@]}" --profile ui up -d --wait --wait-timeout 180 grafana
}

cmd_down() {
  # --profile ui so an on-demand Grafana is stopped too.
  if [[ "${1:-}" == "--clean" ]]; then
    log "stopping and deleting testbed data"
    "${COMPOSE[@]}" --profile ui down -v
  else
    "${COMPOSE[@]}" --profile ui down
  fi
}

cmd_deploy() {
  [[ $# -ge 2 ]] || die "usage: tb.sh deploy SERVICE VERSION [KEY=VALUE ...]"
  local service="$1" version="$2"; shift 2
  local args=("$service" "$version")
  local kv
  for kv in "$@"; do args+=(--config "$kv"); done
  ensure_runs_dir
  "${COMPOSE[@]}" --profile tools run --rm --no-deps deployer "${args[@]}"
}

# Load generator control API (published on 127.0.0.1:8089).
loadgen_api() {
  local method="$1" path="$2" body="${3:-}"
  if [[ -n "$body" ]]; then
    curl -fsS -X "$method" -H 'Content-Type: application/json' -d "$body" "http://127.0.0.1:8089$path"
  else
    curl -fsS -X "$method" "http://127.0.0.1:8089$path"
  fi
  echo
}

cmd_scenario() {
  # Runs inside the controller container, so it reaches the testbed network directly.
  [[ $# -ge 1 ]] || die "usage: tb.sh scenario list | validate | status | run ID [--seed N] [--observe S] ... | start ID | stop ID"
  "${COMPOSE[@]}" exec -T controller python -m harness.controller "$@"
}

cmd_status() {
  "${COMPOSE[@]}" ps -a --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'
}

cmd_logs() {
  "${COMPOSE[@]}" logs --tail 100 -f "$@"
}

cmd_test() {
  cd "$TB_DIR"
  local checks='uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest -q -m "not integration and not scenario"'
  if command -v uv >/dev/null 2>&1; then
    bash -c "$checks"
  else
    log "uv not found; running the checks in a python:3.12-slim container"
    docker run --rm -v "$TB_DIR":/src:ro python:3.12-slim sh -c \
      "pip install -q uv==0.13.8 2>/dev/null && mkdir /work && cd /src && tar --exclude=.venv --exclude=.env -cf - . | tar -C /work -xf - && cd /work && uv sync --locked -q && $checks"
  fi
}

main() {
  local cmd="${1:-}"; shift || true
  case "$cmd" in
    env) ensure_env; log "testbed/.env ready (placeholders left: $(grep -c change-me "$TB_DIR/.env" || true))" ;;
    up) cmd_up ;;
    ui) cmd_ui ;;
    down) cmd_down "$@" ;;
    status) cmd_status ;;
    logs) cmd_logs "$@" ;;
    test) cmd_test ;;
    deploy) cmd_deploy "$@" ;;
    loadgen-stats) loadgen_api GET /stats ;;
    scenario) cmd_scenario "$@" ;;
    loadgen-mix) loadgen_api PUT /mix "{\"name\": \"${1:?usage: tb.sh loadgen-mix NAME}\"}" ;;
    *) die "usage: tb.sh up | ui | down [--clean] | status | logs [service] | test | deploy | scenario | loadgen-stats | loadgen-mix" ;;
  esac
}

main "$@"
