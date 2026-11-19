"""Invariants of testbed/docker-compose.yml (docs/plans/phase-2.md §2.4)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

TESTBED = Path(__file__).resolve().parents[2]
COMPOSE_TEXT = (TESTBED / "docker-compose.yml").read_text(encoding="utf-8")
COMPOSE: dict[str, Any] = yaml.safe_load(COMPOSE_TEXT)
SERVICES: dict[str, dict[str, Any]] = COMPOSE["services"]


def _bytes(value: str) -> int:
    match = re.fullmatch(r"(\d+)([kmg])", str(value).lower())
    assert match, f"unexpected size {value!r}"
    return int(match.group(1)) * {"k": 1024, "m": 1024**2, "g": 1024**3}[match.group(2)]


def test_every_container_has_a_memory_limit_and_no_swap() -> None:
    for name, svc in SERVICES.items():
        assert "mem_limit" in svc, f"{name} has no mem_limit"
        assert svc.get("memswap_limit") == svc["mem_limit"], f"{name} may use swap"


def test_total_memory_budget() -> None:
    total = sum(_bytes(svc["mem_limit"]) for svc in SERVICES.values())
    # Whole testbed (incl. observability backends added later) must stay under ~3.1 GB.
    assert total <= int(3.1 * 1024**3), f"limits sum to {total / 1024**2:.0f} MiB"


def test_ports_bind_to_localhost_only() -> None:
    for name, svc in SERVICES.items():
        for port in svc.get("ports", []):
            assert str(port).startswith("127.0.0.1:"), f"{name} publishes {port} beyond localhost"


def test_gateway_limit_supports_bounded_leak_scenario() -> None:
    assert _bytes(SERVICES["api-gateway"]["mem_limit"]) == 192 * 1024**2


def test_every_compose_variable_is_in_env_example() -> None:
    example = (TESTBED / ".env.example").read_text(encoding="utf-8")
    keys = {line.split("=", 1)[0] for line in example.splitlines() if re.match(r"^[A-Z_]+=", line)}
    used = set(re.findall(r"(?<!\$)\$\{([A-Z_][A-Z0-9_]*)", COMPOSE_TEXT))
    assert used, "no variables found"
    assert used <= keys, f"missing from .env.example: {sorted(used - keys)}"


def test_env_example_has_placeholders_only() -> None:
    example = (TESTBED / ".env.example").read_text(encoding="utf-8")
    for line in example.splitlines():
        if re.match(r"^[A-Z_]*(PASSWORD|TOKEN)=", line):
            assert line.split("=", 1)[1].startswith("change-me"), line


def test_service_image_excludes_harness_and_scenarios() -> None:
    """ADR 0003: the answers never enter the system-under-test image."""
    ignored = (TESTBED / ".dockerignore").read_text(encoding="utf-8").split()
    assert {"harness", "scenarios", "runs", "tests"} <= set(ignored)
    dockerfile = (TESTBED / "Dockerfile").read_text(encoding="utf-8")
    copies = [ln for ln in dockerfile.splitlines() if ln.startswith("COPY") and "--from" not in ln]
    assert all("harness" not in ln and "scenarios" not in ln for ln in copies)


def test_harness_image_excludes_the_services_code() -> None:
    """The harness image needs none of the system under test (and vice versa)."""
    ignored = (TESTBED / "harness.Dockerfile.dockerignore").read_text(encoding="utf-8").split()
    assert {"services", "tests", "runs"} <= set(ignored)
