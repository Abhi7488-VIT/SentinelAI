"""Live checks against a running testbed (Codespaces / CI only — ADR 0004).

    python -m harness.live baseline [--minutes 10] [--warmup 60] [--out runs]
    python -m harness.live deploy-check [--out runs]

Each run writes, under <out>/<check>/:
    <run_id>.json          the run record (inputs, measurements, verdict)
    <run_id>/queries/*.json every backend query with its raw response (evidence)
    <run_id>/summary.md    a short markdown table (also used as the CI job summary)
    latest.json, latest-summary.md
Exit code 0 = PASS, 1 = FAIL, 2 = usage error / check not available yet.

Standard library only, so it runs with plain python3 on a CI runner or in a Codespace.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SERVICES = ("frontend", "api-gateway", "user-service", "order-service", "payment-service")
MAX_BASELINE_ERROR_RATE = 0.005
TRACE_SETTLE_S = 30

LOADGEN = os.environ.get("TESTBED_LOADGEN_URL", "http://127.0.0.1:8089")
PROM = os.environ.get("TESTBED_PROMETHEUS_URL", "http://127.0.0.1:9090")
LOKI = os.environ.get("TESTBED_LOKI_URL", "http://127.0.0.1:3100")
JAEGER = os.environ.get("TESTBED_JAEGER_URL", "http://127.0.0.1:16686")
TB_SH = Path(__file__).resolve().parents[1] / "tb.sh"

PROM_TABLE = {
    "req/s": "sum by (service_name) (rate(http_server_request_duration_seconds_count[{w}]))",
    "5xx share": (
        "(sum by (service_name) (rate(http_server_request_duration_seconds_count"
        '{{http_response_status_code=~"5.."}}[{w}]))'
        " or 0 * sum by (service_name) (rate(http_server_request_duration_seconds_count[{w}])))"
        " / sum by (service_name) (rate(http_server_request_duration_seconds_count[{w}]))"
    ),
    "p50 ms": (
        "1000 * histogram_quantile(0.50, sum by (service_name, le)"
        " (rate(http_server_request_duration_seconds_bucket[{w}])))"
    ),
    "p95 ms": (
        "1000 * histogram_quantile(0.95, sum by (service_name, le)"
        " (rate(http_server_request_duration_seconds_bucket[{w}])))"
    ),
    "pool timeouts": "sum by (service_name) (increase(db_client_connection_timeouts_total[{w}]))",
}


def _say(message: str, *, err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    stream.write(message + "\n")
    stream.flush()


# ---- evidence ------------------------------------------------------------------------------


@dataclass
class Evidence:
    """Every backend query made during a run, with its raw response."""

    directory: Path
    count: int = 0
    index: list[dict[str, str]] = field(default_factory=list)

    def save(self, backend: str, query: str, response: Any) -> Any:
        self.count += 1
        name = f"{self.count:03d}-{backend}.json"
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {"backend": backend, "query": query, "response": response}
        (self.directory / name).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self.index.append({"file": name, "backend": backend, "query": query})
        return response


def http_json(url: str, *, method: str = "GET", timeout: float = 20.0) -> Any:
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"refusing non-HTTP URL: {url!r}")
    req = urllib.request.Request(url, method=method)  # noqa: S310  (scheme checked above)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310  (local backends)
        return json.load(resp)


def prom_query(ev: Evidence, query: str) -> list[dict[str, Any]]:
    url = f"{PROM}/api/v1/query?query={urllib.parse.quote(query)}"
    result: list[dict[str, Any]] = ev.save("prometheus", query, http_json(url))["data"]["result"]
    return result


def loki_query(ev: Evidence, query: str, *, since: str = "30m", limit: int = 50) -> list[Any]:
    params = urllib.parse.urlencode({"query": query, "since": since, "limit": limit})
    url = f"{LOKI}/loki/api/v1/query_range?{params}"
    result: list[Any] = ev.save("loki", query, http_json(url))["data"]["result"]
    return result


def loki_label_values(ev: Evidence, label: str) -> list[str]:
    url = f"{LOKI}/loki/api/v1/label/{label}/values"
    values: list[str] = ev.save("loki", f"label values: {label}", http_json(url)).get("data") or []
    return values


def jaeger(ev: Evidence, path: str, params: dict[str, Any] | None = None) -> Any:
    query = f"{path}?{urllib.parse.urlencode(params)}" if params else path
    return ev.save("jaeger", query, http_json(f"{JAEGER}{query}"))


# ---- pure helpers (unit-tested) ------------------------------------------------------------


def window_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    req = after["requests"] - before["requests"]
    err = after["errors"] - before["errors"]
    by_action = {
        k: after["by_action"].get(k, 0) - before["by_action"].get(k, 0) for k in after["by_action"]
    }
    return {
        "requests": req,
        "errors": err,
        "declined": after["declined"] - before["declined"],
        "error_rate": (err / req) if req else None,
        "by_action": by_action,
    }


def prom_rows(results: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, float]]:
    rows: dict[str, dict[str, float]] = {}
    for column, series in results.items():
        for s in series:
            rows.setdefault(s["metric"].get("service_name", "?"), {})[column] = float(s["value"][1])
    return rows


def baseline_verdict(
    delta: dict[str, Any], presence: dict[str, set[str]]
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not delta["requests"]:
        reasons.append("no requests were recorded in the window")
    elif delta["error_rate"] is not None and delta["error_rate"] >= MAX_BASELINE_ERROR_RATE:
        reasons.append(f"error rate {delta['error_rate']:.3%} >= {MAX_BASELINE_ERROR_RATE:.1%}")
    for backend, seen in presence.items():
        missing = sorted(set(SERVICES) - seen)
        if missing:
            reasons.append(f"{backend} is missing services: {', '.join(missing)}")
    return (not reasons, reasons)


def trace_services(trace: dict[str, Any]) -> list[str]:
    procs = trace.get("processes", {})
    return sorted({procs[sp["processID"]]["serviceName"] for sp in trace.get("spans", [])})


def last_json_line(output: str) -> dict[str, Any]:
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            value: dict[str, Any] = json.loads(line)
            return value
    raise ValueError("no JSON result line in the deploy output")


def fmt(value: float | None, column: str) -> str:
    if value is None or value != value:  # None or NaN
        return "-"
    if column in ("req/s",):
        return f"{value:.2f}"
    if column.endswith("ms"):
        return f"{value:.1f}"
    if column == "5xx share":
        return f"{value:.2%}"
    return f"{value:.0f}"


def render_summary(record: dict[str, Any]) -> str:
    lines = [
        f"### Testbed live check `{record['check']}`: **{'PASS' if record['passed'] else 'FAIL'}**",
        "",
        f"run `{record['run_id']}` · {record['started_at']} → {record['ended_at']} · "
        f"seed {record.get('seed', '-')}",
        "",
    ]
    if record.get("reasons"):
        lines += ["Why it failed:", *[f"- {r}" for r in record["reasons"]], ""]
    if record["check"] == "baseline":
        w = record["window"]
        rate = "-" if w["error_rate"] is None else f"{w['error_rate']:.3%}"
        lines += [
            "| window | requests | req/s | errors | error rate | payment declines |",
            "|---|---|---|---|---|---|",
            f"| {record['minutes']} min | {w['requests']} | {w['requests_per_s']:.2f} | "
            f"{w['errors']} | {rate} | {w['declined']} |",
            "",
            "| service | " + " | ".join(PROM_TABLE) + " |",
            "|---|" + "---|" * len(PROM_TABLE),
        ]
        for svc in sorted(record["prometheus"]):
            row = record["prometheus"][svc]
            lines.append(f"| {svc} | " + " | ".join(fmt(row.get(c), c) for c in PROM_TABLE) + " |")
        lines += ["", "| backend | services seen |", "|---|---|"]
        for backend, seen in record["presence"].items():
            lines.append(f"| {backend} | {', '.join(sorted(seen))} |")
    else:
        lines += ["| step | result |", "|---|---|"]
        for step in record["steps"]:
            lines.append(f"| {step['name']} | {step['result']} |")
    lines += ["", f"Evidence: {record['evidence_files']} query outputs in the artifact."]
    return "\n".join(lines) + "\n"


# ---- checks --------------------------------------------------------------------------------


def run_baseline(ev: Evidence, minutes: int, warmup_s: int) -> dict[str, Any]:
    _say(f"[live] warm-up {warmup_s}s")
    time.sleep(warmup_s)
    before = http_json(f"{LOADGEN}/stats")
    ev.save("loadgen", "GET /stats (window start)", before)
    t0 = time.time()
    for minute in range(1, minutes + 1):
        time.sleep(60)
        _say(f"[live] minute {minute}/{minutes}")
    after = http_json(f"{LOADGEN}/stats")
    ev.save("loadgen", "GET /stats (window end)", after)
    seconds = time.time() - t0
    delta = window_delta(before["totals"], after["totals"])
    delta["requests_per_s"] = delta["requests"] / seconds
    delta["dropped"] = after["dropped"] - before["dropped"]

    w = f"{minutes}m"
    prom = prom_rows({col: prom_query(ev, q.format(w=w)) for col, q in PROM_TABLE.items()})
    prom_services = {
        s["metric"].get("service_name", "")
        for s in prom_query(
            ev, "count by (service_name) (http_server_request_duration_seconds_count)"
        )
    }
    jaeger_services = set(jaeger(ev, "/api/services").get("data") or [])
    # Only traces that ended >= TRACE_SETTLE_S ago: each service exports its spans in batches,
    # so the newest traces are still being assembled (seen live: 2 of 5 had only frontend spans
    # at query time and all 20 spans a minute later).
    now_us = int(time.time() * 1e6)
    traces = (
        jaeger(
            ev,
            "/api/traces",
            {
                "service": "frontend",
                "operation": "POST /checkout",
                "limit": 5,
                "start": now_us - int(seconds * 1e6),
                "end": now_us - TRACE_SETTLE_S * 1_000_000,
            },
        ).get("data")
        or []
    )
    per_trace = [trace_services(tr) for tr in traces]
    complete = [svcs for svcs in per_trace if set(svcs) == set(SERVICES)]
    chain = sorted(set().union(*per_trace)) if per_trace else []
    loki_services = set(loki_label_values(ev, "service_name"))
    presence = {"prometheus": prom_services, "jaeger": jaeger_services, "loki": loki_services}
    passed, reasons = baseline_verdict(delta, presence)
    if traces and len(complete) != len(traces):
        passed = False
        partial = [svcs for svcs in per_trace if set(svcs) != set(SERVICES)]
        reasons.append(
            f"{len(partial)} of {len(traces)} settled checkout traces do not span all five "
            f"services: {partial}"
        )
    if not traces:
        passed = False
        reasons.append("no checkout trace found in Jaeger")
    return {
        "check": "baseline",
        "minutes": minutes,
        "window": delta,
        "prometheus": prom,
        "presence": {k: sorted(v) for k, v in presence.items()},
        "checkout_trace_services": chain,
        "checkout_traces_complete": f"{len(complete)}/{len(traces)}",
        "seed": after.get("seed"),
        "mix": after.get("mix"),
        "passed": passed,
        "reasons": reasons,
    }


def poll(fn: Callable[[], bool], timeout_s: float = 90.0, every_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(every_s)
    return False


def tb_deploy(service: str, version: str) -> dict[str, Any]:
    proc = subprocess.run(  # noqa: S603
        ["bash", str(TB_SH), "deploy", service, version],  # noqa: S607  (bash from PATH)
        capture_output=True,
        text=True,
        check=False,
    )
    sys.stdout.write(proc.stdout[-2000:])
    sys.stderr.write(proc.stderr[-2000:])
    return last_json_line(proc.stdout)


def run_deploy_check(ev: Evidence) -> dict[str, Any]:
    steps: list[dict[str, str]] = []
    reasons: list[str] = []

    def step(name: str, ok: bool, detail: str) -> None:
        steps.append({"name": name, "result": f"{'ok' if ok else 'FAILED'} — {detail}"})
        if not ok:
            reasons.append(f"{name}: {detail}")

    out = tb_deploy("payment-service", "1.2.1")
    ev.save("deploy", "tb.sh deploy payment-service 1.2.1", out)
    step(
        "deploy payment-service 1.2.1",
        out.get("status") == "succeeded",
        f"status {out.get('status')} in {out.get('seconds')}s "
        f"(previous {out.get('previous_version')})",
    )
    deployment_id = str(out.get("deployment_id", ""))

    version_query = (
        "count by (service_version) (http_server_request_duration_seconds_count"
        '{service_name="payment-service"})'
    )
    seen_version = poll(
        lambda: any(
            s["metric"].get("service_version") == "1.2.1" for s in prom_query(ev, version_query)
        )
    )
    step(
        "Prometheus shows service_version=1.2.1",
        seen_version,
        "payment-service series labelled 1.2.1",
    )

    def deploy_events() -> list[str]:
        streams = loki_query(ev, '{service_name="deploy-pipeline"}', since="15m", limit=100)
        return [
            f"{line} [{st['stream'].get('deployment_status')}]"
            for st in streams
            if st["stream"].get("deployment_id") == deployment_id
            for _, line in st["values"]
        ]

    found: list[str] = []

    def both_events() -> bool:
        found[:] = deploy_events()
        return any("started" in e for e in found) and any("finished" in e for e in found)

    step("Loki has both deploy events", poll(both_events), "; ".join(sorted(found)) or "none")

    back = tb_deploy("payment-service", "1.2.0")
    ev.save("deploy", "tb.sh deploy payment-service 1.2.0", back)
    step("rollback to 1.2.0", back.get("status") == "succeeded", f"status {back.get('status')}")
    return {"check": "deploy-check", "steps": steps, "passed": not reasons, "reasons": reasons}


# ---- entry point ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness.live")
    parser.add_argument("check", help="baseline | deploy-check | 1-5 (scenarios, from T6)")
    parser.add_argument("--minutes", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=60)
    parser.add_argument("--out", type=Path, default=Path("runs"))
    args = parser.parse_args(argv)

    if args.check in {"1", "2", "3", "4", "5"}:
        _say(f"[live] scenario {args.check} is not implemented yet (T6-T8)", err=True)
        return 2
    if args.check not in {"baseline", "deploy-check"}:
        _say(f"[live] unknown check {args.check!r}", err=True)
        return 2
    if not 1 <= args.minutes <= 30:
        _say("[live] --minutes must be between 1 and 30", err=True)
        return 2

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    folder = args.out / args.check
    ev = Evidence(folder / run_id / "queries")
    started = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        if args.check == "baseline":
            record = run_baseline(ev, args.minutes, args.warmup)
        else:
            record = run_deploy_check(ev)
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        record = {
            "check": args.check,
            "passed": False,
            "reasons": [f"check crashed: {exc!r}"],
            "steps": [],
            "window": None,
        }
    record.update(
        run_id=run_id,
        started_at=started,
        ended_at=datetime.now(UTC).isoformat(timespec="seconds"),
        evidence_files=ev.count,
        evidence_index=ev.index,
    )
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{run_id}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    shutil.copyfile(folder / f"{run_id}.json", folder / "latest.json")
    if record.get("window") is not None or record["check"] == "deploy-check":
        summary = render_summary(record)
    else:
        summary = (
            f"### Testbed live check `{args.check}`: **FAIL**\n\n"
            + "\n".join(f"- {r}" for r in record["reasons"])
            + "\n"
        )
    (folder / run_id).mkdir(parents=True, exist_ok=True)
    (folder / run_id / "summary.md").write_text(summary, encoding="utf-8")
    (folder / "latest-summary.md").write_text(summary, encoding="utf-8")
    _say(summary)
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
