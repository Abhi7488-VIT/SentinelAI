"""Operations shared by the CLI and the HTTP API (both call these; no logic is duplicated)."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

import httpx

from harness.controller import groundtruth
from harness.controller.lock import RunLock
from harness.controller.plugin import discover
from harness.controller.primitives import Context, ControllerSettings
from harness.controller.runner import Run, RunOptions, iso, new_run_id


class BusyError(Exception):
    def __init__(self, holder: dict[str, Any]) -> None:
        super().__init__(f"another run holds the lock: {holder}")
        self.holder = holder


def list_scenarios(settings: ControllerSettings) -> list[dict[str, Any]]:
    plugins = discover()
    out = []
    for sid in groundtruth.definition_ids(settings.scenarios_dir):
        d = groundtruth.load_definition(settings.scenarios_dir, sid)
        out.append(
            {
                "id": sid,
                "number": d.get("number"),
                "title": d["title"],
                "category": d["root_cause"]["category"],
                "has_plugin": sid in plugins,
            }
        )
    return sorted(out, key=lambda s: (s["number"] or 99, s["id"]))


def prepare_run(
    settings: ControllerSettings, scenario_id: str, options: RunOptions
) -> tuple[Run, RunLock]:
    """Validate, then take the lock. Raises BusyError (with the holder) if a run is active."""
    run = Run(settings, scenario_id, options)
    lock = RunLock(settings.runs_dir)
    holder = lock.acquire(
        {"run_id": run.run_id, "scenario_id": scenario_id, "mode": options.mode, "pid": os.getpid()}
    )
    if holder is not None:
        raise BusyError(holder)
    return run, lock


async def execute(
    run: Run, lock: RunLock, on_step: Callable[[dict[str, str]], None] | None = None
) -> dict[str, Any]:
    try:
        return await run.execute(on_step)
    finally:
        lock.release(run.run_id)


async def manual_start(settings: ControllerSettings, scenario_id: str, seed: int) -> dict[str, Any]:
    """Inject a fault and leave it in place (no checks, no verdict) until manual_stop."""
    definition = groundtruth.load_definition(settings.scenarios_dir, scenario_id)
    plugin = discover()[scenario_id]
    lock = RunLock(settings.runs_dir)
    run_id = new_run_id()
    holder = lock.acquire(
        {"run_id": run_id, "scenario_id": scenario_id, "mode": "manual", "pid": os.getpid()}
    )
    if holder is not None:
        if holder.get("scenario_id") == scenario_id and holder.get("mode") == "manual":
            found = groundtruth.find_record(settings.runs_dir, str(holder["run_id"]))
            if found is not None:
                return found  # idempotent: already started
        raise BusyError(holder)
    record = groundtruth.new_record(definition, _manual_run(run_id, seed))
    async with httpx.AsyncClient() as client:
        ctx = Context(settings, client, seed, record["run"]["steps"])
        now = iso(time.time())
        record["timeline"] = {"started_at": now, "ended_at": now}
        try:
            await plugin.trigger(ctx)
        except Exception:
            await plugin.stop(ctx)
            lock.release(run_id)
            raise
    record["run"]["reasons"].append("manual run: no symptom checks, no verdict")
    groundtruth.write_record(settings.runs_dir, record, settings.scenarios_dir)
    return record


async def manual_stop(settings: ControllerSettings, scenario_id: str) -> dict[str, Any]:
    """Stop a scenario: ends an active full run early, or removes a manually started fault."""
    lock = RunLock(settings.runs_dir)
    holder = lock.current()
    if holder and holder.get("scenario_id") == scenario_id and holder.get("mode") != "manual":
        lock.request_stop(str(holder["run_id"]))
        return {"stop_requested": holder["run_id"]}
    plugin = discover()[scenario_id]
    async with httpx.AsyncClient() as client:
        steps: list[dict[str, str]] = []
        ctx = Context(settings, client, settings.default_seed, steps)
        await plugin.stop(ctx)
    if holder and holder.get("scenario_id") == scenario_id:
        record = groundtruth.find_record(settings.runs_dir, str(holder["run_id"]))
        if record is not None:
            record["timeline"]["ended_at"] = iso(time.time())
            record["run"]["steps"].extend(steps)
            groundtruth.write_record(settings.runs_dir, record, settings.scenarios_dir)
        lock.release(str(holder["run_id"]))
        return record or {"stopped": scenario_id}
    return {"stopped": scenario_id, "steps": steps}


def _manual_run(run_id: str, seed: int) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "seed": seed,
        "status": "manual",
        "mode": "manual",
        "windows": {"baseline_s": 0, "observe_s": 0, "recovery_s": 0},
        "baseline_started_at": None,
        "recovery_ended_at": None,
        "baseline_healthy": None,
        "baseline_checks": [],
        "observed": [],
        "recovery": [],
        "fingerprint": None,
        "memory": {},
        "reasons": [],
        "steps": [],
    }
