"""Fault controller: checks, ground truth, allow-lists, lock, lifecycle, HTTP (no testbed)."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import httpx
import jsonschema
import pytest
from fastapi.testclient import TestClient

from harness.controller import groundtruth, service
from harness.controller.api import build_app
from harness.controller.checks import (
    Check,
    Condition,
    compare_fingerprints,
    effective_threshold,
    evaluate_points,
    evaluate_traces,
    fingerprint,
    magnitude_bucket,
    reduce_max,
)
from harness.controller.lock import RunLock
from harness.controller.plugin import Scenario, discover
from harness.controller.primitives import (
    Context,
    ControllerSettings,
    NotAllowedError,
    deploy,
    network_link,
    restart_service,
    sql_change,
    traffic_pattern,
)
from harness.controller.runner import Run, RunOptions
from harness.loadgen.mixes import MIXES

TESTBED = Path(__file__).resolve().parents[2]
SCENARIOS = TESTBED / "scenarios"
T0 = 1_800_000_000.0


# ---- checks --------------------------------------------------------------------------------


def _pts(values: list[float], start: float = T0) -> list[tuple[float, float]]:
    return [(start + 15 * i, v) for i, v in enumerate(values)]


def test_reduce_max_merges_series_and_drops_nan() -> None:
    series = [
        {"values": [[T0, "1"], [T0 + 15, "NaN"]]},
        {"values": [[T0, "3"], [T0 + 15, "2"], [T0 + 30, "+Inf"]]},
    ]
    assert reduce_max(series) == [(T0, 3.0), (T0 + 15, 2.0)]


def test_sustained_needs_consecutive_points() -> None:
    cond = Condition(op=">", threshold=0, min_points=3)
    scattered = evaluate_points("x", _pts([1, 0, 1, 0, 1]), cond, 0, T0)
    assert not scattered.observed
    assert scattered.points_met == 3
    sustained = evaluate_points("x", _pts([0, 0, 1, 1, 1, 0]), cond, 0, T0)
    assert sustained.observed
    assert sustained.onset_s == 30
    assert sustained.peak == 1


def test_fraction_rule_and_empty_series() -> None:
    cond = Condition(op="<", threshold=0.01, min_fraction=1.0)
    assert evaluate_points("x", _pts([0, 0.001, 0]), cond, 0.01, T0).observed
    assert not evaluate_points("x", _pts([0, 0.02, 0]), cond, 0.01, T0).observed
    assert not evaluate_points("x", [], cond, 0.01, T0).observed  # no data is never "fine"


def test_relative_threshold_uses_baseline_with_floor() -> None:
    cond = Condition(op=">=", threshold=5, relative_to_baseline=True, min_value=0.025)
    assert effective_threshold(cond, 0.010) == pytest.approx(0.05)
    assert effective_threshold(cond, 0.001) == pytest.approx(0.025)
    assert effective_threshold(cond, None) == pytest.approx(0.025)


def test_trace_check_counts_and_onset() -> None:
    traces = [
        {
            "processes": {
                "p1": {"serviceName": "order-service"},
                "p2": {"serviceName": "frontend"},
            },
            "spans": [
                {"processID": "p2", "startTime": int((T0 + 5) * 1e6)},
                {"processID": "p1", "startTime": int((T0 + 42) * 1e6)},
            ],
        }
    ] * 3
    obs = evaluate_traces("t", traces, "order-service", Condition(op=">=", threshold=3), T0)
    assert obs.observed
    assert obs.onset_s == 42
    assert not evaluate_traces("t", traces, "order-service", Condition(">=", 4), T0).observed


def test_fingerprint_and_tolerance() -> None:
    checks = [
        Check("a", "metric", "prometheus", "", "q", Condition(">", 0.2)),
        Check("b", "metric", "prometheus", "", "q", Condition("<", 0.01)),
    ]
    obs = [
        evaluate_points("a", _pts([0, 0.1, 0.5, 0.9]), Condition(">", 0.2), 0.2, T0),
        evaluate_points("b", _pts([0, 0, 0]), Condition("<", 0.01, min_fraction=1), 0.01, T0),
    ]
    fp = fingerprint(obs, checks)
    assert fp == {
        "symptoms": [
            {"id": "a", "onset_bucket": 2, "magnitude_bucket": 2},  # 0.9 / 0.2 = 4.5 -> log2 = 2
            {"id": "b", "onset_bucket": 0, "magnitude_bucket": None},
        ]
    }
    later = copy.deepcopy(fp)
    later["symptoms"][0]["onset_bucket"] = 3
    assert compare_fingerprints(fp, later) == []  # ±1 bucket of scheduling jitter
    later["symptoms"][0]["onset_bucket"] = 4
    assert compare_fingerprints(fp, later)
    assert compare_fingerprints(fp, {"symptoms": fp["symptoms"][:1]})
    assert magnitude_bucket(100, 1, ">") == 4  # clamped


# ---- ground truth --------------------------------------------------------------------------


def test_every_definition_is_valid_and_has_a_plugin() -> None:
    ids = groundtruth.definition_ids(SCENARIOS)
    assert ids, "no scenario definitions"
    plugins = discover()
    assert set(ids) == set(plugins), "every definition needs a plugin and vice versa"
    numbers = []
    for sid in ids:
        d = groundtruth.load_definition(SCENARIOS, sid)
        numbers.append(d["number"])
        for check in d["expected_symptoms"] + d["recovery_checks"]:
            Check.from_json(check)  # every check is implementable
            if check["backend"] == "jaeger":
                assert isinstance(check["query"], dict), check["id"]
            else:
                assert isinstance(check["query"], str), check["id"]
        assert d["root_cause"]["service"] in d["expected_affected_services"]
        assert not set(d["expected_affected_services"]) & set(d["unaffected_services"])
        if d["root_cause"]["code_location"]:
            assert (TESTBED / d["root_cause"]["code_location"]["path"]).is_file()
    assert len(numbers) == len(set(numbers))


def test_definitions_carry_no_times_and_records_need_them() -> None:
    d = groundtruth.load_definition(SCENARIOS, "db-pool-exhaustion")
    bad = copy.deepcopy(d)
    bad["timeline"]["started_at"] = "2026-11-17T10:00:00Z"
    with pytest.raises(jsonschema.ValidationError):
        groundtruth.validate(bad, SCENARIOS)
    record = groundtruth.new_record(d, {"run_id": "x"})
    with pytest.raises(jsonschema.ValidationError):
        groundtruth.validate(record, SCENARIOS)  # incomplete run + null times


def test_scenario_mixes_exist_in_the_load_generator() -> None:
    from harness.controller.primitives import ALLOWED_MIXES

    assert set(MIXES) >= ALLOWED_MIXES


def test_run_ids_cannot_traverse(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid run id"):
        groundtruth.find_record(tmp_path, "../../etc/passwd")
    with pytest.raises(ValueError, match="invalid scenario id"):
        groundtruth.load_definition(SCENARIOS, "../x")


# ---- allow-lists ---------------------------------------------------------------------------


async def test_primitives_refuse_targets_outside_the_allow_list(tmp_path: Path) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as c:
        ctx = Context(ControllerSettings(runs_dir=tmp_path), c, 42)
        with pytest.raises(NotAllowedError):
            await traffic_pattern(ctx, "drop-database")
        with pytest.raises(NotAllowedError):
            await sql_change(ctx, "DROP TABLE orders")
        with pytest.raises(NotAllowedError):
            await network_link(ctx, "postgres", enabled=False)
        with pytest.raises(NotAllowedError):
            await restart_service(ctx, "postgres")
        with pytest.raises(NotAllowedError):
            await deploy(ctx, "prometheus", "9.9.9")
        assert ctx.steps == []


def test_controller_is_not_instrumented() -> None:
    """Faults must show up only through their effects, never as controller telemetry."""
    for path in (TESTBED / "harness" / "controller").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "opentelemetry" not in text, path
        assert "chaos" not in text.lower(), path


# ---- lock ----------------------------------------------------------------------------------


def test_lock_is_exclusive_and_recovers_stale(tmp_path: Path) -> None:
    lock = RunLock(tmp_path)
    assert lock.acquire({"run_id": "r1", "scenario_id": "s"}) is None
    holder = lock.acquire({"run_id": "r2", "scenario_id": "s"})
    assert holder is not None
    assert holder["run_id"] == "r1"
    lock.request_stop("r1")
    assert lock.stop_requested("r1")
    assert not lock.stop_requested("r2")
    lock.release("r1")
    assert lock.current() is None
    # A lock left by a dead process is stale.
    lock.path.write_text(json.dumps({"run_id": "old", "pid": 2**22 + 7}), encoding="utf-8")
    assert lock.acquire({"run_id": "r3", "scenario_id": "s"}) is None


# ---- lifecycle with fake backends ----------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.5)


class FakePlugin(Scenario):
    id = "db-pool-exhaustion"

    def __init__(self, clock: FakeClock, *, fail_trigger: bool = False) -> None:
        self.clock = clock
        self.calls: list[str] = []
        self.window: list[float] = []
        self.fail_trigger = fail_trigger

    async def reset(self, ctx: Context) -> None:
        self.calls.append("reset")

    async def trigger(self, ctx: Context) -> None:
        self.calls.append("trigger")
        self.window = [self.clock(), float("inf")]
        if self.fail_trigger:
            raise RuntimeError("boom")

    async def stop(self, ctx: Context) -> None:
        self.calls.append("stop")
        if self.window:
            self.window[1] = self.clock()


def fake_backends(definition: dict[str, Any], plugin: FakePlugin, *, missing: str = "") -> Any:
    fault: dict[str, float] = {}
    for c in definition["expected_symptoms"]:
        if isinstance(c["query"], str) and c["id"] != missing:
            cond = c["condition"]
            up = cond["op"] in (">", ">=")
            fault[c["query"]] = cond["threshold"] * 2 + 1 if up else cond["threshold"] / 2

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "loadgen":
            return httpx.Response(200, json={"mix": "x", "seed": 42})
        params = dict(url.params)
        if url.path.endswith("query_range"):
            scale = 1e9 if url.host == "loki" else 1.0
            start, end = float(params["start"]) / scale, float(params["end"]) / scale
            query = params["query"]
            values = []
            t = start
            while t <= end:
                active = bool(plugin.window) and plugin.window[0] <= t <= plugin.window[1]
                values.append([t, str(fault.get(query, 0.0) if active else 0.0)])
                t += 15
            return httpx.Response(
                200, json={"data": {"resultType": "matrix", "result": [{"values": values}]}}
            )
        if url.path == "/api/traces":
            start = float(params["start"]) / 1e6
            active = bool(plugin.window) and start <= plugin.window[0] + 1
            if not active or missing == f"{params['service'].split('-')[0]}-error-traces":
                return httpx.Response(200, json={"data": []})
            tr = {
                "processes": {"p": {"serviceName": params["service"]}},
                "spans": [{"processID": "p", "startTime": int((plugin.window[0] + 20) * 1e6)}],
            }
            return httpx.Response(200, json={"data": [tr] * 10})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _run(
    tmp_path: Path,
    *,
    mode: str = "full",
    missing: str = "",
    mem: int | None = 8000,
    fail_trigger: bool = False,
) -> tuple[dict[str, Any], FakePlugin]:
    import asyncio

    clock = FakeClock()
    plugin = FakePlugin(clock, fail_trigger=fail_trigger)
    settings = ControllerSettings(runs_dir=tmp_path, scenarios_dir=SCENARIOS)
    definition = groundtruth.load_definition(SCENARIOS, plugin.id)
    client = httpx.AsyncClient(transport=fake_backends(definition, plugin, missing=missing))
    run = Run(
        settings,
        plugin.id,
        RunOptions(seed=7, baseline_s=60, observe_s=120, recovery_s=60, mode=mode),
        plugins={plugin.id: plugin},
        client=client,
        clock=clock,
        sleep=clock.sleep,
        meminfo=lambda: mem,
    )
    return asyncio.run(run.execute()), plugin


def test_full_run_is_verified_and_written(tmp_path: Path) -> None:
    record, plugin = _run(tmp_path)
    run = record["run"]
    assert run["status"] == "verified", run["reasons"]
    assert plugin.calls == ["reset", "trigger", "stop"]
    assert run["baseline_healthy"] is True
    assert all(o["observed"] for o in run["observed"])
    assert all(o["observed"] for o in run["recovery"])
    assert record["timeline"]["started_at"] < record["timeline"]["ended_at"]
    saved = json.loads((tmp_path / plugin.id / "latest.json").read_text(encoding="utf-8"))
    groundtruth.validate(saved, SCENARIOS)
    assert saved["run"]["run_id"] == run["run_id"]
    assert saved["run"]["fingerprint"]["symptoms"]
    evidence = list((tmp_path / plugin.id / run["run_id"] / "queries").iterdir())
    assert len(evidence) >= len(record["expected_symptoms"]) + 2 * len(record["recovery_checks"])
    assert [s["step"] for s in run["steps"]][:2] == ["reset", "traffic"]


def test_missing_symptom_fails_the_run(tmp_path: Path) -> None:
    record, _ = _run(tmp_path, missing="order-pool-timeout-logs")
    assert record["run"]["status"] == "failed"
    assert "order-pool-timeout-logs" in record["run"]["reasons"][0]


def test_dry_run_never_injects_and_writes_a_valid_record(tmp_path: Path) -> None:
    record, plugin = _run(tmp_path, mode="dry_run")
    assert record["run"]["status"] == "dry_run"
    assert "trigger" not in plugin.calls
    assert "stop" not in plugin.calls
    groundtruth.validate(record, SCENARIOS)
    upward = [o for o in record["run"]["observed"] if o["id"] != "payment-unaffected"]
    assert not any(o["observed"] for o in upward)


def test_memory_guard_aborts_before_the_trigger(tmp_path: Path) -> None:
    record, plugin = _run(tmp_path, mem=100)
    assert record["run"]["status"] == "aborted"
    assert "memory guard" in record["run"]["reasons"][0]
    assert "trigger" not in plugin.calls


def test_a_failed_trigger_still_removes_the_fault(tmp_path: Path) -> None:
    record, plugin = _run(tmp_path, fail_trigger=True)
    assert record["run"]["status"] == "failed"
    assert plugin.calls[-1] == "stop"
    assert record["timeline"]["ended_at"] is not None


def test_unhealthy_baseline_means_no_injection(tmp_path: Path) -> None:
    clock = FakeClock()
    plugin = FakePlugin(clock)
    plugin.window = [0, float("inf")]  # symptoms already present before the trigger
    settings = ControllerSettings(runs_dir=tmp_path, scenarios_dir=SCENARIOS)
    definition = groundtruth.load_definition(SCENARIOS, plugin.id)
    # Healthy-check queries return fault values all the time.
    transport = fake_backends(
        {
            "expected_symptoms": definition["recovery_checks"]
            and [
                dict(c, condition={"op": ">", "threshold": 5})
                for c in definition["recovery_checks"]
            ]
        },
        plugin,
    )
    import asyncio

    run = Run(
        settings,
        plugin.id,
        RunOptions(baseline_s=60, observe_s=60, recovery_s=60),
        plugins={plugin.id: plugin},
        client=httpx.AsyncClient(transport=transport),
        clock=clock,
        sleep=clock.sleep,
        meminfo=lambda: 8000,
    )
    record = asyncio.run(run.execute())
    assert record["run"]["status"] == "failed"
    assert record["run"]["baseline_healthy"] is False
    assert "trigger" not in plugin.calls
    groundtruth.validate(record, SCENARIOS)


def test_run_options_bounds() -> None:
    with pytest.raises(ValueError, match="observe"):
        RunOptions(observe_s=5).validate()
    with pytest.raises(ValueError, match="mode"):
        RunOptions(mode="yolo").validate()


# ---- HTTP ----------------------------------------------------------------------------------


def test_http_requires_token_and_start_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = ControllerSettings(
        runs_dir=tmp_path, scenarios_dir=SCENARIOS, controller_token="t0k"
    )
    # Pretend a run of scenario 1 is in progress (held by this live process).
    RunLock(tmp_path).acquire(
        {
            "run_id": "20261117T000000Z-abcdef",
            "scenario_id": "db-pool-exhaustion",
            "pid": os.getpid(),
        }
    )
    api = TestClient(build_app(settings))
    assert api.get("/healthz").status_code == 200
    assert api.get("/scenarios").status_code == 401
    assert api.get("/scenarios", headers={"Authorization": "Bearer nope"}).status_code == 401
    auth = {"Authorization": "Bearer t0k"}
    listed = api.get("/scenarios", headers=auth).json()
    assert listed[0]["id"] == "db-pool-exhaustion"
    assert listed[0]["has_plugin"]
    again = api.post("/scenarios/db-pool-exhaustion/start", json={}, headers=auth)
    assert again.status_code == 202
    assert again.json() == {"run_id": "20261117T000000Z-abcdef", "already_running": True}
    assert api.post("/scenarios/nope/start", json={}, headers=auth).status_code == 404
    assert api.get("/runs/..%2F..%2Fx", headers=auth).status_code == 404
    assert api.post("/scenarios/db-pool-exhaustion/stop", headers=auth).json() == {
        "stop_requested": "20261117T000000Z-abcdef"
    }


def test_http_refuses_to_start_without_a_token(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        build_app(ControllerSettings(runs_dir=tmp_path, scenarios_dir=SCENARIOS))


def test_busy_lock_blocks_a_second_run(tmp_path: Path) -> None:
    settings = ControllerSettings(runs_dir=tmp_path, scenarios_dir=SCENARIOS)
    RunLock(tmp_path).acquire({"run_id": "r", "scenario_id": "other", "pid": os.getpid()})
    with pytest.raises(service.BusyError):
        service.prepare_run(settings, "db-pool-exhaustion", RunOptions())


def test_onset_is_never_negative_zero() -> None:
    cond = Condition.from_json({"op": "<", "threshold": 1, "min_fraction": 1.0})
    obs = evaluate_points("x", [(99.99999, 0.0)], cond, 1.0, 100.0)
    assert obs.onset_s == 0.0
    assert str(obs.onset_s) == "0.0"


def test_cli_start_and_stop_reach_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: `stop` has no --seed option and used to crash before removing the fault."""
    from harness.controller import __main__ as cli
    from harness.controller import service as svc

    calls: list[tuple[str, str, int | None]] = []

    async def fake_start(settings: object, sid: str, seed: int) -> dict[str, Any]:
        calls.append(("start", sid, seed))
        return {"run": {"run_id": "r"}}

    async def fake_stop(settings: object, sid: str) -> dict[str, Any]:
        calls.append(("stop", sid, None))
        return {"stopped": sid}

    monkeypatch.setattr(svc, "manual_start", fake_start)
    monkeypatch.setattr(svc, "manual_stop", fake_stop)
    monkeypatch.setenv("SEED", "7")
    assert cli.main(["start", "db-pool-exhaustion", "--seed", "3"]) == 0
    assert cli.main(["start", "db-pool-exhaustion"]) == 0
    assert cli.main(["stop", "db-pool-exhaustion"]) == 0
    assert calls == [
        ("start", "db-pool-exhaustion", 3),
        ("start", "db-pool-exhaustion", 7),
        ("stop", "db-pool-exhaustion", None),
    ]
