#!/usr/bin/env bash
# Runs once when the Codespace is created. Installs tools and dependencies only:
# nothing is started (run `make testbed-up` yourself when you need the testbed).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "[post-create] make"
if ! command -v make >/dev/null 2>&1; then
  sudo apt-get update -y
  sudo apt-get install -y --no-install-recommends make
fi
make --version | head -1

echo "[post-create] uv 0.13.8"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  python3 -m pip install --user --no-cache-dir uv==0.13.8
fi
uv --version

echo "[post-create] backend dependencies"
(cd backend && uv sync --locked)

echo "[post-create] testbed dependencies"
(cd testbed && uv sync --locked)

echo "[post-create] Claude Code CLI"
npm install -g @anthropic-ai/claude-code
claude --version || true

echo "[post-create] done. Nothing was started. Next: see docs/codespaces.md"
