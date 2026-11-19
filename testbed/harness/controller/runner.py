"""Scenario run lifecycle.

    reset -> baseline traffic (seeded) -> baseline window -> baseline checks
          -> trigger -> observe window -> symptom checks
          -> stop -> recovery window -> recovery checks -> verdict + run record

A run is `verified` only if the baseline was healthy, every expected symptom was observed in
the observe window and every recovery check held at the end of the recovery window. Anything
else is `failed` (or `aborted` if the memory guard or a stop request cut it short); never
assumed. The fault is always removed (stop runs in `finally` once a trigger was attempted).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from harness.controller import groundtruth
from harness.controller.backends import Backends, Endpoints, Evidence
from harness.controller.checks import Check, Observation, fingerprint
from harness.controller.lock import RunLock
from harness.controller.plugin import Scenario, discover
from harness.controller.primitives import Context, ControllerSettings, traffic_pattern
from harness.memguard import parse_mem_available_mb

TICK_S = 1.0


@dataclass(frozen=True)
class RunOptions:
    seed: int = 42
    baseline_s: int = 120
    observe_s: int = 300
    recovery_s: int = 180  # judged on the 2nd half: 90 s > the checks' 1 min lookback
    mode: str = "full"  # full | dry_run

    def validate(self) -> None:
        if self.mode not in ("full", "dry_run"):
            raise ValueError(f"unknown mode {self.mode!r}")
        if not 30 <= self.baseline_s <= 900:
            raise ValueError("baseline window must be 30-900 s")
        if not 30 <= self.observe_s <= 900:
            raise ValueError("observe window must be 30-900 s")
        if not 60 <= self.recovery_s <= 900:
            raise ValueError("recovery window must be 60-900 s")


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def mem_available_mb(meminfo: Path = Path("/proc/meminfo")) -> int | None:
    try:
        return parse_mem_available_mb(meminfo.read_text(encoding="ascii"))
    except (OSError, ValueError):
        return None


class AbortedError(Exception):
    pass


class Run:
    def __init__(
        self,
        settings: ControllerSettings,
        scenario_id: str,
        options: RunOptions,
        *,
        run_id: str | None = None,
        plugins: dict[str, Scenario] | None = None,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Any] = asyncio.sleep,
        meminfo: Callable[[], int | None] = mem_available_mb,
    ) -> None:
        options.validate()
        self.settings = settings
        self.options = options
        self.definition = groundtruth.load_definition(settings.scenarios_dir, scenario_id)
        plugins = discover() if plugins is None else plugins
        if scenario_id not in plugins:
            raise KeyError(f"no plugin for scenario {scenario_id!r}")
        self.plugin = plugins[scenario_id]
        self.run_id = run_id or new_run_id()
        self.client = client
        self.clock = clock
        self.sleep = sleep
        self.meminfo = meminfo
        self.lock = RunLock(settings.runs_dir)
        self.symptoms = [Check.from_json(c) for c in self.definition["expected_symptoms"]]
        self.recovery_checks = [Check.from_json(c) for c in self.definition["recovery_checks"]]
        self.evidence = Evidence(settings.runs_dir / scenario_id / self.run_id / "queries")
        self.min_mem: int | None = None
        self.run: dict[str, Any] = {
            "run_id": self.run_id,
            "seed": options.seed,
            "status": "running",
            "mode": options.mode,
            "windows": {
                "baseline_s": options.baseline_s,
                "observe_s": options.observe_s,
                "recovery_s": options.recovery_s,
            },
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
        self.record = groundtruth.new_record(self.definition, self.run)
        self.record["timeline"] = {"started_at": None, "ended_at": None}

    # ---- helpers ------------------------------------------------------------------------

    def _save(self) -> Path:
        self.run["memory"] = {
            "min_available_mb": self.min_mem,
            "abort_below_mb": self.settings.min_available_mb,
        }
        # A run record needs times; until the trigger they are the baseline start.
        tl = self.record["timeline"]
        placeholder = self.run["baseline_started_at"] or iso(self.clock())
        saved = dict(tl)
        if tl["started_at"] is None:
            tl["started_at"] = placeholder
        if tl["ended_at"] is None:
            tl["ended_at"] = tl["started_at"]
        try:
            return groundtruth.write_record(
                self.settings.runs_dir, self.record, self.settings.scenarios_dir
            )
        finally:
            self.record["timeline"] = saved

    async def _wait(self, seconds: float) -> None:
        """Sleep in ticks, checking the memory guard and stop requests."""
        end = self.clock() + seconds
        while self.clock() < end:
            avail = self.meminfo()
            if avail is not None:
                self.min_mem = avail if self.min_mem is None else min(self.min_mem, avail)
                if avail < self.settings.min_available_mb:
                    raise AbortedError(
                        f"memory guard: MemAvailable {avail} MiB"
                        f" < {self.settings.min_available_mb} MiB"
                    )
            if self.lock.stop_requested(self.run_id):
                raise AbortedError("stop requested")
            await self.sleep(min(TICK_S, max(0.0, end - self.clock())))

    async def _evaluate(
        self,
        backends: Backends,
        checks: list[Check],
        start: float,
        end: float,
        baselines: dict[str, float | None] | None = None,
    ) -> list[Observation]:
        return [await backends.evaluate(c, start, end, (baselines or {}).get(c.id)) for c in checks]

    # ---- lifecycle ----------------------------------------------------------------------

    async def execute(
        self, on_step: Callable[[dict[str, str]], None] | None = None
    ) -> dict[str, Any]:
        async with self.client or httpx.AsyncClient() as client:
            ctx = Context(self.settings, client, self.options.seed, self.run["steps"], on_step)
            backends = Backends(
                Endpoints(
                    self.settings.prometheus_url, self.settings.loki_url, self.settings.jaeger_url
                ),
                self.evidence,
                client,
            )
            try:
                await self._lifecycle(ctx, backends)
            except AbortedError as exc:
                self.run["status"] = "aborted"
                self.run["reasons"].append(str(exc))
                ctx.step("abort", str(exc))
            except Exception as exc:
                self.run["status"] = "failed"
                self.run["reasons"].append(f"controller error: {exc!r}"[:500])
                ctx.step("error", repr(exc)[:300])
            finally:
                tl = self.record["timeline"]
                if tl["started_at"] is not None and tl["ended_at"] is None:
                    # Safety: never leave a fault in place.
                    try:
                        await self.plugin.stop(ctx)
                        ctx.step("stop", "fault removed after an abort/error")
                    except Exception as exc:
                        self.run["reasons"].append(f"stop failed: {exc!r}"[:300])
                    self.record["timeline"]["ended_at"] = iso(self.clock())
                    with_baseline = await traffic_pattern_safe(ctx)
                    if not with_baseline:
                        self.run["reasons"].append("could not restore baseline traffic")
                if tl["started_at"] is None:
                    # Never injected: record a zero-length fault window at the point it stopped.
                    tl["started_at"] = tl["ended_at"] = iso(self.clock())
                self._save()
        return self.record

    async def _lifecycle(self, ctx: Context, backends: Backends) -> None:
        opt = self.options
        ctx.step("reset", f"scenario {self.definition['id']} ({opt.mode})")
        await self.plugin.reset(ctx)
        await traffic_pattern(ctx, "baseline")
        t_base = self.clock()
        self.run["baseline_started_at"] = iso(t_base)
        self._save()

        await self._wait(opt.baseline_s)
        t_trigger = self.clock()
        # Relative thresholds (symptoms and recovery checks) are measured on this run's own
        # baseline window; a symptom and a recovery check may share an id-less query, so the
        # map is keyed by check id.
        baselines = {
            c.id: await backends.baseline_of(c, t_base, t_trigger)
            for c in [*self.symptoms, *self.recovery_checks]
            if c.condition.relative_to_baseline
        }
        baseline_obs = await self._evaluate(
            backends, self.recovery_checks, t_base, t_trigger, baselines
        )
        self.run["baseline_checks"] = [o.to_json() for o in baseline_obs]
        self.run["baseline_healthy"] = all(o.observed for o in baseline_obs)
        if not self.run["baseline_healthy"]:
            bad = [o.id for o in baseline_obs if not o.observed]
            self.run["status"] = "failed"
            self.run["reasons"].append(f"baseline unhealthy before the trigger: {', '.join(bad)}")
            ctx.step("baseline", f"unhealthy: {', '.join(bad)} — fault NOT injected")
            return
        ctx.step("baseline", "healthy: " + ", ".join(o.id for o in baseline_obs))

        self.record["timeline"]["started_at"] = iso(t_trigger)
        if opt.mode == "full":
            await self.plugin.trigger(ctx)
        else:
            ctx.step("trigger", "dry run: fault NOT injected")
        self._save()
        await self._wait(opt.observe_s)
        t_obs_end = self.clock()
        observed = await self._evaluate(backends, self.symptoms, t_trigger, t_obs_end, baselines)
        self.run["observed"] = [o.to_json() for o in observed]
        ctx.step(
            "observe",
            ", ".join(f"{o.id}={'yes' if o.observed else 'no'}" for o in observed),
        )

        if opt.mode == "full":
            await self.plugin.stop(ctx)
        t_stop = self.clock()
        self.record["timeline"]["ended_at"] = iso(t_stop)
        self._save()
        await self._wait(opt.recovery_s)
        t_end = self.clock()
        self.run["recovery_ended_at"] = iso(t_end)
        recovery = await self._evaluate(
            backends, self.recovery_checks, t_stop + opt.recovery_s / 2, t_end, baselines
        )
        self.run["recovery"] = [o.to_json() for o in recovery]
        ctx.step("recovery", ", ".join(f"{o.id}={'ok' if o.observed else 'NO'}" for o in recovery))

        self.run["fingerprint"] = fingerprint(observed, self.symptoms)
        missing = [o.id for o in observed if not o.observed]
        not_recovered = [o.id for o in recovery if not o.observed]
        if opt.mode == "dry_run":
            self.run["status"] = "dry_run"
            return
        if missing:
            self.run["reasons"].append(f"expected symptoms not observed: {', '.join(missing)}")
        if not_recovered:
            self.run["reasons"].append(f"did not return to baseline: {', '.join(not_recovered)}")
        self.run["status"] = "verified" if not (missing or not_recovered) else "failed"


async def traffic_pattern_safe(ctx: Context) -> bool:
    try:
        await traffic_pattern(ctx, "baseline")
    except (httpx.HTTPError, ValueError):
        return False
    return True
