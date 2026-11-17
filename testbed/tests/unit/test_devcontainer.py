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
COMMANDS = [
    ln.strip() for ln in POST_CREATE.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
]


def test_host_requirements_are_at_least_4_cpus_and_16gb() -> None:
    req = DEVCONTAINER["hostRequirements"]
    assert req["cpus"] >= 4
    assert int(re.fullmatch(r"(\d+)gb", req["memory"]).group(1)) >= 16  # type: ignore[union-attr]


def test_base_image_has_no_yarn_repo() -> None:
    """The python:*-bookworm image ships a Yarn apt repo whose key fails (NO_PUBKEY
    62D54FD4003F6525), which made the docker-in-docker feature exit 100."""
    assert DEVCONTAINER["image"] == "mcr.microsoft.com/devcontainers/base:ubuntu"


def test_tooling_features() -> None:
    features = DEVCONTAINER["features"]
    assert features["ghcr.io/devcontainers/features/python:1"] == {"version": "3.12"}
    assert features["ghcr.io/devcontainers/features/node:1"] == {"version": "lts"}
    assert "ghcr.io/devcontainers/features/docker-in-docker:2" in features


def test_ports_forwarded_with_labels() -> None:
    expected = {8081, 8089, 9090, 3100, 16686, 3000, 8000}
    assert expected <= set(DEVCONTAINER["forwardPorts"])
    for port in DEVCONTAINER["forwardPorts"]:
        assert DEVCONTAINER["portsAttributes"][str(port)]["label"]


def test_nothing_starts_automatically() -> None:
    for hook in ("postStartCommand", "postAttachCommand", "initializeCommand"):
        assert hook not in DEVCONTAINER
    assert DEVCONTAINER["postCreateCommand"] == "bash .devcontainer/post-create.sh"
    assert not any(
        re.search(r"compose\s+up|make\s+testbed-up|tb\.sh up", ln) and not ln.startswith("echo")
        for ln in COMMANDS
    )


def test_yarn_repo_removed_before_any_apt_call() -> None:
    removal = next(
        i for i, ln in enumerate(COMMANDS) if "rm -f /etc/apt/sources.list.d/yarn.list" in ln
    )
    first_apt = next(i for i, ln in enumerate(COMMANDS) if "apt-get" in ln)
    assert removal < first_apt
    assert COMMANDS[removal].endswith("|| true")  # missing file is fine


def test_required_steps_are_not_masked() -> None:
    assert "set -euo pipefail" in COMMANDS
    for required in (
        "apt-get install -y --no-install-recommends make",
        "uv==0.13.8",
        "(cd testbed && uv sync --locked)",
    ):
        line = next(ln for ln in COMMANDS if required in ln)
        assert "|| true" not in line, line


def test_optional_steps_never_fail_setup_and_are_reported() -> None:
    assert any(ln.startswith('optional "backend dependencies') for ln in COMMANDS)
    assert any(ln.startswith('optional "Claude Code CLI') for ln in COMMANDS)
    assert "npm install -g @anthropic-ai/claude-code" in POST_CREATE
    assert "SKIPPED (failed, not fatal)" in POST_CREATE


def test_no_secrets_in_devcontainer() -> None:
    text = json.dumps(DEVCONTAINER) + POST_CREATE
    assert not re.search(r"(?i)(api[_-]?key|secret|password|token)\s*[\"']?\s*[:=]", text)
    assert "sk-ant-" not in text
