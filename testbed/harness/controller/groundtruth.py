"""Ground-truth definitions (committed, times null) and runtime records (runs/, gitignored)."""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import jsonschema

SCHEMA_FILE = "schema/ground-truth.schema.json"
RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")
SCENARIO_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def schema(scenarios_dir: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((scenarios_dir / SCHEMA_FILE).read_text(encoding="utf-8"))
    return data


def validate(document: dict[str, Any], scenarios_dir: Path) -> None:
    """Raises jsonschema.ValidationError if the document does not match the schema."""
    jsonschema.Draft202012Validator(
        schema(scenarios_dir), format_checker=jsonschema.FormatChecker()
    ).validate(document)


def definition_ids(scenarios_dir: Path) -> list[str]:
    return sorted(p.stem for p in scenarios_dir.glob("*.json"))


def load_definition(scenarios_dir: Path, scenario_id: str) -> dict[str, Any]:
    if not SCENARIO_ID.match(scenario_id):
        raise ValueError(f"invalid scenario id {scenario_id!r}")
    path = scenarios_dir / f"{scenario_id}.json"
    doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    validate(doc, scenarios_dir)
    if doc["kind"] != "definition" or doc["id"] != scenario_id:
        raise ValueError(f"{path.name} is not the definition of {scenario_id}")
    return doc


def new_record(definition: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    record = copy.deepcopy(definition)
    record["kind"] = "run"
    record["run"] = run
    return record


def write_record(runs_dir: Path, record: dict[str, Any], scenarios_dir: Path) -> Path:
    validate(record, scenarios_dir)
    folder: Path = runs_dir / str(record["id"])
    folder.mkdir(parents=True, exist_ok=True)
    path: Path = folder / f"{record['run']['run_id']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    shutil.copyfile(path, folder / "latest.json")
    return path


def find_record(runs_dir: Path, run_id: str) -> dict[str, Any] | None:
    if not RUN_ID.match(run_id):
        raise ValueError(f"invalid run id {run_id!r}")
    for path in runs_dir.glob(f"*/{run_id}.json"):
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data
    return None
