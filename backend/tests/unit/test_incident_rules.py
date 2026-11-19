"""Incident title, severity and status-transition rules (pure)."""

from __future__ import annotations

import pytest

from app.services.correlation import incident_title, max_severity
from app.services.incidents import TRANSITIONS, can_transition


def test_titles_are_derived_from_rules_and_services() -> None:
    assert incident_title(["error_rate"], ["order-service"]) == "Errors in order-service"
    assert (
        incident_title(
            ["error_rate", "latency_shift", "error_rate"],
            ["order-service", "api-gateway", "frontend"],
        )
        == "Errors and latency in order-service, api-gateway, frontend"
    )
    assert incident_title(
        ["health_check", "resource_saturation", "deploy_change"], ["a", "b", "c", "d", "e"]
    ) == ("Health check failures, resource saturation and a deployment change in a, b, c +2 more")


def test_severity_is_the_maximum() -> None:
    assert max_severity(["warning", "critical", "warning"]) == "critical"
    assert max_severity(["warning"]) == "warning"


@pytest.mark.parametrize(
    ("current", "target", "ok"),
    [
        ("open", "investigating", True),
        ("investigating", "mitigated", True),
        ("mitigated", "resolved", True),
        ("open", "resolved", True),
        ("investigating", "resolved", True),
        ("resolved", "open", True),
        ("open", "mitigated", False),
        ("open", "open", False),
        ("mitigated", "open", False),
        ("resolved", "investigating", False),
        ("mitigated", "investigating", False),
    ],
)
def test_transitions(current: str, target: str, ok: bool) -> None:
    assert can_transition(current, target) is ok


def test_every_status_can_reach_resolved_or_reopen() -> None:
    assert set(TRANSITIONS) == {"open", "investigating", "mitigated", "resolved"}
