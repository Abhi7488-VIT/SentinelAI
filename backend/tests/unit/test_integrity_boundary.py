"""ADR 0003 guard: SentinelAI's code never reads the testbed's answers.

The backend may learn about the system under test only from telemetry (and, in Phase 5, from
`testbed/services/` source code). It must never reference ground-truth definitions, run records,
the harness, or any scenario by id — otherwise detection/correlation results would be inflated.
Only harness-side evaluation compares SentinelAI output with ground truth.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
SCENARIOS_DIR = REPO_ROOT / "testbed" / "scenarios"
# Production code and schema history. Tests are excluded: this file names the terms itself.
# `.txt`: versioned LLM prompts (Phase 4) must not name scenarios either.
GUARDED_SUFFIXES = {".py", ".json", ".sql", ".txt"}
GUARDED_DIRS = [BACKEND_DIR / "app", BACKEND_DIR / "migrations", BACKEND_DIR / "scripts"]

FORBIDDEN_TERMS = [
    "testbed/scenarios",
    "testbed/runs",
    "testbed/harness",
    "harness.",
    "ground_truth",
    "ground-truth",
    "groundtruth",
    "expected_affected_services",
    "expected_symptoms",
    "injected_fault",
]


def _scenario_ids() -> list[str]:
    ids = []
    for path in sorted(SCENARIOS_DIR.glob("*.json")):
        ids.append(str(json.loads(path.read_text(encoding="utf-8"))["id"]))
    return ids


def _guarded_files() -> list[Path]:
    files: list[Path] = []
    for directory in GUARDED_DIRS:
        files.extend(p for p in directory.rglob("*") if p.suffix in GUARDED_SUFFIXES)
    return files


def test_scenario_definitions_are_found() -> None:
    # Sanity: the guard is meaningless if it cannot see the ids it protects.
    assert SCENARIOS_DIR.is_dir()
    assert len(_scenario_ids()) >= 5


def test_backend_never_references_ground_truth_or_scenarios() -> None:
    terms = [*FORBIDDEN_TERMS, *_scenario_ids()]
    pattern = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
    offenders: dict[str, list[str]] = {}
    for path in _guarded_files():
        hits = sorted(set(pattern.findall(path.read_text(encoding="utf-8"))))
        if hits:
            offenders[str(path.relative_to(BACKEND_DIR))] = hits
    assert not offenders, f"backend code references testbed answers: {offenders}"
