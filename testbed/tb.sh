#!/usr/bin/env bash
# SentinelAI testbed workflow. The repo-root Makefile calls this; it also works on its own:
#   testbed/tb.sh up | down [--clean] | status | logs [service] | test
# Later tasks add: scenario-list | scenario-run | scenario-start | scenario-stop | deploy
set -euo pipefail

TB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose -f "$TB_DIR/docker-compose.yml" --env-file "$TB_DIR/.env")

log() { printf '[testbed] %s\n' "$*"; }
die() { printf '[testbed] error: %s\n' "$*" >&2; exit 1; }

random_secret() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 16
  else head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}

ensure_env() {
  if [[ -f "$TB_DIR/.env" ]]; then return; fi
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

cmd_up() {
  ensure_env
  warn_if_sentinelai_running
  log "building and starting (waits until every service is healthy)"
  "${COMPOSE[@]}" up --build -d --wait --wait-timeout 300
  cmd_status
}

cmd_down() {
  if [[ "${1:-}" == "--clean" ]]; then
    log "stopping and deleting testbed data"
    "${COMPOSE[@]}" down -v
  else
    "${COMPOSE[@]}" down
  fi
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
    up) cmd_up ;;
    down) cmd_down "$@" ;;
    status) cmd_status ;;
    logs) cmd_logs "$@" ;;
    test) cmd_test ;;
    *) die "usage: tb.sh up | down [--clean] | status | logs [service] | test" ;;
  esac
}

main "$@"
