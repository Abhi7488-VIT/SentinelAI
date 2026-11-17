"""Read-only clients for Prometheus, Loki and Jaeger. Every query and its raw response is saved
as an evidence file, so a run record's claims can be checked against what the backends said."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from harness.controller.checks import (
    STEP_S,
    Check,
    Observation,
    baseline_value,
    effective_threshold,
    evaluate_points,
    evaluate_traces,
    reduce_max,
)


@dataclass
class Evidence:
    directory: Path
    count: int = 0
    files: list[str] = field(default_factory=list)

    def save(self, backend: str, query: Any, params: dict[str, Any], response: Any) -> str:
        self.count += 1
        name = f"{self.count:03d}-{backend}.json"
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {"backend": backend, "query": query, "params": params, "response": response}
        (self.directory / name).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.files.append(name)
        return name


@dataclass(frozen=True)
class Endpoints:
    prometheus: str = "http://prometheus:9090"
    loki: str = "http://loki:3100"
    jaeger: str = "http://jaeger:16686"


class Backends:
    def __init__(self, endpoints: Endpoints, evidence: Evidence, client: httpx.AsyncClient) -> None:
        self.ep = endpoints
        self.evidence = evidence
        self.client = client

    async def _get(
        self, backend: str, url: str, params: dict[str, Any], query: Any
    ) -> tuple[Any, str]:
        resp = await self.client.get(url, params=params, timeout=30)
        body: Any = resp.json()
        name = self.evidence.save(backend, query, params, body)
        resp.raise_for_status()
        return body, name

    async def range_series(
        self, backend: str, query: str, start: float, end: float
    ) -> tuple[list[dict[str, Any]], str]:
        if backend == "prometheus":
            url = f"{self.ep.prometheus}/api/v1/query_range"
        elif backend == "loki":
            url = f"{self.ep.loki}/loki/api/v1/query_range"
        else:
            raise ValueError(f"{backend} has no range queries")
        params: dict[str, Any] = {"query": query, "step": f"{STEP_S}s"}
        if backend == "prometheus":
            params.update(start=f"{start:.3f}", end=f"{end:.3f}")
        else:  # Loki takes nanosecond epochs
            params.update(start=str(int(start * 1e9)), end=str(int(end * 1e9)))
        body, name = await self._get(backend, url, params, query)
        data = body.get("data", {})
        if data.get("resultType") not in ("matrix", None):
            raise ValueError(f"{backend} query must return a matrix, got {data.get('resultType')}")
        return list(data.get("result") or []), name

    async def traces(
        self, query: dict[str, Any], start: float, end: float, limit: int = 50
    ) -> tuple[list[dict[str, Any]], str]:
        params: dict[str, Any] = {
            "service": query["service"],
            "start": str(int(start * 1e6)),
            "end": str(int(end * 1e6)),
            "limit": str(limit),
        }
        if query.get("operation"):
            params["operation"] = query["operation"]
        if query.get("tags"):
            params["tags"] = json.dumps(query["tags"], sort_keys=True)
        if query.get("min_duration_ms") is not None:
            params["minDuration"] = f"{int(query['min_duration_ms'])}ms"
        body, name = await self._get("jaeger", f"{self.ep.jaeger}/api/traces", params, query)
        return list(body.get("data") or []), name

    # ---- check evaluation ---------------------------------------------------------------

    async def baseline_of(self, check: Check, start: float, end: float) -> float | None:
        if check.backend == "jaeger" or not check.condition.relative_to_baseline:
            return None
        if not isinstance(check.query, str):
            raise ValueError(f"a {check.backend} check needs a string query")
        series, _ = await self.range_series(check.backend, check.query, start, end)
        return baseline_value(reduce_max(series))

    async def evaluate(
        self, check: Check, start: float, end: float, baseline: float | None = None
    ) -> Observation:
        try:
            if check.backend == "jaeger":
                if not isinstance(check.query, dict):
                    raise ValueError("a jaeger check needs an object query")
                traces, name = await self.traces(check.query, start, end)
                obs = evaluate_traces(
                    check.id, traces, check.query["service"], check.condition, start
                )
            else:
                if not isinstance(check.query, str):
                    raise ValueError(f"a {check.backend} check needs a string query")
                series, name = await self.range_series(check.backend, check.query, start, end)
                threshold = effective_threshold(check.condition, baseline)
                obs = evaluate_points(
                    check.id, reduce_max(series), check.condition, threshold, start
                )
            obs.baseline = baseline
            obs.evidence = [name]
            return obs
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            return Observation(
                id=check.id,
                observed=False,
                threshold=check.condition.threshold,
                points=0,
                points_met=0,
                peak=None,
                onset_s=None,
                baseline=baseline,
                detail=f"query failed: {exc!r}"[:500],
                evidence=self.evidence.files[-1:],
            )
