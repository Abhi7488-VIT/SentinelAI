"""The Codespaces devcontainer meets the live-verification requirements (ADR 0004)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
DEVCONTAINER: dict[str, Any] = json.loads(
    (REPO / ".devcontainer" / "devcontainer.json").read_text(encoding="utf-8")
)
POST_CREATE = (REPO / ".devcontainer" / "post-create.sh").read_text(encoding="utf-8")


def test_host_requirements_are_at_least_4_cpus_and_16gb() -> None:
    req = DEVCONTAINER["hostRequirements"]
    assert req["cpus"] >= 4
    assert int(re.fullmatch(r"(\d+)gb", req["memory"]).group(1)) >= 16  # type: ignore[union-attr]


def test_tooling_features() -> None:
    features = DEVCONTAINER["features"]
    assert any("docker-in-docker" in f for f in features)
    assert any(f.startswith("ghcr.io/devcontainers/features/node") for f in features)
    assert "3.12" in DEVCONTAINER["image"]


def test_ports_forwarded_with_labels() -> None:
    expected = {8081, 8089, 9090, 3100, 16686, 3000, 8000}
    assert expected <= set(DEVCONTAINER["forwardPorts"])
    for port in DEVCONTAINER["forwardPorts"]:
        assert DEVCONTAINER["portsAttributes"][str(port)]["label"]


def test_nothing_starts_automatically() -> None:
    for hook in ("postStartCommand", "postAttachCommand", "initializeCommand"):
        assert hook not in DEVCONTAINER
    assert DEVCONTAINER["postCreateCommand"] == "bash .devcontainer/post-create.sh"
    commands = [ln for ln in POST_CREATE.splitlines() if not ln.lstrip().startswith("#")]
    assert not any(
        re.search(r"compose\s+up|make\s+testbed-up|tb\.sh up", ln) and "echo" not in ln
        for ln in commands
    )


def test_post_create_installs_dependencies_and_claude_code() -> None:
    assert "uv==0.13.8" in POST_CREATE
    assert "(cd backend && uv sync --locked)" in POST_CREATE
    assert "(cd testbed && uv sync --locked)" in POST_CREATE
    assert "npm install -g @anthropic-ai/claude-code" in POST_CREATE


def test_no_secrets_in_devcontainer() -> None:
    text = json.dumps(DEVCONTAINER) + POST_CREATE
    assert not re.search(r"(?i)(api[_-]?key|secret|password|token)\s*[\"']?\s*[:=]", text)
    assert "sk-ant-" not in text
