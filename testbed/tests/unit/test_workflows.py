"""Contract of the GitHub workflows that matter for the testbed (structure only).

The workflows are also linted by actionlint (with shellcheck) in the CI `actionlint` job.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[3]


def _load(name: str) -> dict[Any, Any]:
    data: dict[Any, Any] = yaml.safe_load(
        (REPO / ".github" / "workflows" / name).read_text(encoding="utf-8")
    )
    return data


LIVE = _load("testbed-live.yml")
CI = _load("ci.yml")


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    on = workflow.get("on", workflow.get(True))  # YAML 1.1 reads the `on` key as True
    assert isinstance(on, dict)
    return on


def _steps(workflow: dict[Any, Any], job: str) -> dict[str, dict[str, Any]]:
    return {s.get("name", s.get("uses", "")): s for s in workflow["jobs"][job]["steps"]}


def test_live_workflow_is_manual_only_with_inputs() -> None:
    on = _triggers(LIVE)
    assert set(on) == {"workflow_dispatch"}
    inputs = on["workflow_dispatch"]["inputs"]
    assert inputs["scenario_id"]["default"] == "baseline"
    assert inputs["duration_minutes"]["default"] == "10"


def test_live_workflow_limits_and_permissions() -> None:
    assert LIVE["permissions"] == {"contents": "read"}
    job = LIVE["jobs"]["live"]
    assert 0 < job["timeout-minutes"] <= 90
    assert job["env"]["TB_MIN_AVAILABLE_MB"]


def test_live_workflow_always_cleans_up_and_reports() -> None:
    steps = _steps(LIVE, "live")
    for name in ("Collect diagnostics", "Stop the testbed", "Upload artifacts", "Job summary"):
        assert steps[name]["if"] == "always()", name
    assert "make testbed-down" in steps["Stop the testbed"]["run"]
    assert "GITHUB_STEP_SUMMARY" in steps["Job summary"]["run"]
    assert steps["Upload artifacts"]["with"]["path"] == "testbed/runs/"


def test_live_workflow_runs_the_check_under_the_watchdog() -> None:
    run = _steps(LIVE, "live")["Run the check (under the memory watchdog)"]["run"]
    assert "harness.memguard watch" in run
    assert "harness.live" in run
    names = list(_steps(LIVE, "live"))
    assert names.index("Generate testbed/.env with random secrets") < names.index(
        "Start the testbed"
    )


def test_ci_lints_workflows_and_runs_testbed_unit_tests() -> None:
    assert "actionlint" in CI["jobs"]
    assert "rhysd/actionlint" in str(CI["jobs"]["actionlint"]["steps"])
    testbed_steps = str(CI["jobs"]["testbed"]["steps"])
    assert "mypy" in testbed_steps
    assert "not integration and not scenario" in testbed_steps
