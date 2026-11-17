#!/usr/bin/env bash
# Runs once when the Codespace is created. Installs tools and dependencies only:
# nothing is started (run `make testbed-up` yourself when you need the testbed).
#
# Required steps (make, uv, testbed dependencies) fail the setup if they fail.
# Optional steps (backend dependencies, Claude Code CLI) never do: they are reported as SKIPPED.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

SKIPPED=()
optional() {  # optional "description" command...
  local what="$1"; shift
  if "$@"; then
    echo "[post-create] ok: $what"
  else
    echo "[post-create] SKIPPED (failed, not fatal): $what"
    SKIPPED+=("$what")
  fi
}

# Defense in depth: a stale Yarn apt repo (NO_PUBKEY 62D54FD4003F6525) breaks every `apt-get update`.
# Note: dev container features install before this script runs, so this only protects the apt
# calls below; the image choice (base:ubuntu, no Yarn repo) is what keeps the features working.
sudo rm -f /etc/apt/sources.list.d/yarn.list || true

echo "[post-create] make (required)"
if ! command -v make >/dev/null 2>&1; then
  sudo apt-get update -y
  sudo apt-get install -y --no-install-recommends make
fi
make --version | head -1

echo "[post-create] uv 0.13.8 (required)"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  if command -v pipx >/dev/null 2>&1; then
    pipx install uv==0.13.8
  else
    python3 -m pip install --user --no-cache-dir uv==0.13.8
  fi
fi
uv --version
python3 --version

echo "[post-create] testbed dependencies (required)"
(cd testbed && uv sync --locked)

echo "[post-create] backend dependencies (optional)"
optional "backend dependencies (cd backend && uv sync --locked)" \
  bash -c "cd backend && uv sync --locked"

echo "[post-create] Claude Code CLI (optional)"
optional "Claude Code CLI (npm install -g @anthropic-ai/claude-code)" \
  npm install -g @anthropic-ai/claude-code
claude --version 2>/dev/null || true

echo
if ((${#SKIPPED[@]})); then
  echo "[post-create] finished with ${#SKIPPED[@]} skipped optional step(s):"
  printf '  - %s\n' "${SKIPPED[@]}"
  echo "  Re-run them by hand from the repository root."
else
  echo "[post-create] finished: all steps succeeded."
fi
echo "[post-create] Nothing was started. Next: see docs/codespaces.md"
