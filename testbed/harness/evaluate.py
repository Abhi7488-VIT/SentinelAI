"""Score SentinelAI's incidents for a scenario run against the run's ground truth (ADR 0003).

    python -m harness.evaluate incidents --run RUN_ID [--runs-dir runs] [--project shop]
                                         [--api http://127.0.0.1:8000] [--out FILE]

This is the only place SentinelAI output meets ground truth, and it sits on the harness side:
it reads the run record from `runs/` and talks to SentinelAI **only through its public HTTP API**
as a normal user (credentials from SENTINELAI_EMAIL / SENTINELAI_PASSWORD, or the repo-root
.env). SentinelAI's backend never imports this module or reads ground truth.

Checks (fixed before any run; nothing here is tuned to a result):

- `exactly_one_incident`  one incident whose start lies in the fault window, not several;
- `expected_services_affected`  every `expected_affected_services` entry is an affected service;
- `unaffected_services_excluded`  no `unaffected_services` entry is an affected service;
- `grouping_is_structural`  every detection that joined carries at least one grouping reason.
  Only the incident's opener is exempt, and the opener is the `detection_id` of the incident's
  `created` event (not the first detection in the list: the API orders detections by
  `(started_at, id)`, so detections that start together sort by UUID). With no `created` event
  no detection is exempt;
- `detected_during_fault`  the first detection fired before the fault ended;
- `explanations_readable`  every explanation passes mechanical readability checks (below);
- `no_false_positives`  no run detection is a false positive.

A detection is a **false positive** if it started during the run and either is on a service the
ground truth does not list as affected, or started outside [fault start, fault end + 60 s]
(60 s = the detectors' trailing window, so a symptom still inside the window at fault end
counts as caused by the fault).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from harness.controller.groundtruth import find_record

TRAILING_WINDOW = timedelta(seconds=60)
EXPLANATION_MIN, EXPLANATION_MAX = 40, 1200
_TEMPLATE_LEFTOVERS = re.compile(r"[{}]|\bNone\b|\bnan\b|\bnull\b")
_NUMBER = re.compile(r"\d")
_TESTBED_DIR = Path(__file__).resolve().parents[1]


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def explanation_problems(service: str, text: str) -> list[str]:
    """Mechanical readability checks. A human still reads the quoted text in the report."""
    problems = []
    if not EXPLANATION_MIN <= len(text) <= EXPLANATION_MAX:
        problems.append(f"length {len(text)} outside {EXPLANATION_MIN}..{EXPLANATION_MAX}")
    if not text.startswith(f"{service}:"):
        problems.append("does not start with '<service>:'")
    if not _NUMBER.search(text):
        problems.append("contains no number")
    if _TEMPLATE_LEFTOVERS.search(text):
        problems.append("contains template leftovers ({, }, None, nan, null)")
    if not text.isprintable():
        problems.append("contains non-printable characters")
    return problems


@dataclass(frozen=True)
class ApiData:
    """What the evaluator needs from SentinelAI, as returned by its public API."""

    services: dict[str, str]  # service id -> name (the project's services)
    incidents: list[dict[str, Any]]  # incident details (with detections, reasons and `events`)
    detections: list[dict[str, Any]]  # every detection that started during the run


def opener_detection_id(incident: dict[str, Any]) -> str | None:
    """The detection that opened the incident, from its first `created` event (None if absent)."""
    for event in sorted(incident.get("events", []), key=lambda e: e["id"]):
        if event["type"] == "created":
            opener = event["payload"].get("detection_id")
            return str(opener) if opener is not None else None
    return None


def _check(name: str, passed: bool, expected: Any, actual: Any) -> dict[str, Any]:
    return {"check": name, "passed": passed, "expected": expected, "actual": actual}


def evaluate_run(record: dict[str, Any], data: ApiData) -> dict[str, Any]:
    run = record["run"]
    fault_start = ts(record["timeline"]["started_at"])
    fault_end = ts(record["timeline"]["ended_at"])
    run_start, run_end = ts(run["baseline_started_at"]), ts(run["recovery_ended_at"])
    expected = set(record["expected_affected_services"])
    unaffected = set(record["unaffected_services"])
    name = data.services.get

    in_fault = [i for i in data.incidents if fault_start <= ts(i["started_at"]) < fault_end]
    other_incidents = [i for i in data.incidents if i not in in_fault]
    incident = in_fault[0] if len(in_fault) == 1 else None

    def det_view(d: dict[str, Any]) -> dict[str, Any]:
        service = name(d["service_id"], d["service_id"])
        return {
            "id": d["id"],
            "service": service,
            "rule": d["rule"],
            "severity": d["severity"],
            "metric": d["metric"],
            "observed_value": d["observed_value"],
            "threshold": d["threshold"],
            "started_at": d["started_at"],
            "fired_at": d["fired_at"],
            "resolved_at": d["resolved_at"],
            "started_after_fault_s": (ts(d["started_at"]) - fault_start).total_seconds(),
            "fired_after_fault_s": (ts(d["fired_at"]) - fault_start).total_seconds(),
            "explanation": d["explanation"],
            "explanation_problems": explanation_problems(service, d["explanation"]),
            "sample_trace_ids": d["evidence"].get("sample_trace_ids", [])[:5],
        }

    members: list[dict[str, Any]] = []
    affected: list[str] = []
    opener_id = opener_detection_id(incident) if incident is not None else None
    if incident is not None:
        affected = sorted(s["name"] for s in incident["services"])
        for m in incident["detections"]:
            members.append(
                {**det_view(m["detection"]), "reasons": m["reasons"], "score": m["score"]}
            )

    false_positives = []
    for d in data.detections:
        started = ts(d["started_at"])
        if not run_start <= started <= run_end:
            continue
        service = name(d["service_id"], d["service_id"])
        why = []
        if service not in expected:
            why.append(
                "service listed as unaffected" if service in unaffected else "service not affected"
            )
        if not fault_start <= started <= fault_end + TRAILING_WINDOW:
            why.append("started outside the fault window")
        if why:
            false_positives.append({**det_view(d), "why": why})

    first_fired = min((ts(m["fired_at"]) for m in members), default=None)
    joined = [m for m in members if m["id"] != opener_id]
    checks = [
        _check("exactly_one_incident", len(in_fault) == 1, 1, len(in_fault)),
        _check(
            "expected_services_affected",
            incident is not None and expected <= set(affected),
            sorted(expected),
            affected,
        ),
        _check(
            "unaffected_services_excluded",
            incident is not None and not (unaffected & set(affected)),
            f"none of {sorted(unaffected)}",
            sorted(unaffected & set(affected)),
        ),
        _check(
            "grouping_is_structural",
            incident is not None and all(m["reasons"] for m in joined),
            "every joined detection has >= 1 reason",
            {m["id"]: len(m["reasons"]) for m in joined},
        ),
        _check(
            "detected_during_fault",
            first_fired is not None and first_fired < fault_end,
            f"first detection fired before {fault_end.isoformat()}",
            first_fired.isoformat() if first_fired else None,
        ),
        _check(
            "explanations_readable",
            bool(members) and not any(m["explanation_problems"] for m in members),
            "no problems",
            {m["id"]: m["explanation_problems"] for m in members if m["explanation_problems"]},
        ),
        _check("no_false_positives", not false_positives, 0, len(false_positives)),
    ]
    metrics = {
        "time_to_first_detection_s": (
            (first_fired - fault_start).total_seconds() if first_fired else None
        ),
        "time_to_incident_s": (
            (ts(incident["created_at"]) - fault_start).total_seconds() if incident else None
        ),
        "incident_detected_after_fault_s": (
            (ts(incident["detected_at"]) - fault_start).total_seconds() if incident else None
        ),
        "detections_in_incident": len(members),
        "false_positive_detections": len(false_positives),
        "other_incidents_during_run": len(other_incidents),
        "root_cause_service_has_detection": any(
            m["service"] == record["root_cause"]["service"] for m in members
        ),
    }
    return {
        "scenario": record["id"],
        "run_id": run["run_id"],
        "run_status": run["status"],
        "fault_window": {"start": fault_start.isoformat(), "end": fault_end.isoformat()},
        "run_window": {"start": run_start.isoformat(), "end": run_end.isoformat()},
        "ground_truth": {
            "expected_affected_services": sorted(expected),
            "unaffected_services": sorted(unaffected),
            "root_cause_service": record["root_cause"]["service"],
        },
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
        "metrics": metrics,
        "incident": (
            {
                k: incident[k]
                for k in (
                    "id",
                    "number",
                    "title",
                    "status",
                    "severity",
                    "started_at",
                    "detected_at",
                    "created_at",
                )
            }
            | {"affected_services": affected}
            if incident
            else None
        ),
        "opener_detection_id": opener_id,
        "incident_detections": members,
        "incidents_in_fault_window": [
            {"number": i["number"], "title": i["title"], "started_at": i["started_at"]}
            for i in in_fault
        ],
        "other_incidents_during_run": [
            {"number": i["number"], "title": i["title"], "started_at": i["started_at"]}
            for i in other_incidents
        ],
        "false_positives": false_positives,
    }


# ---- SentinelAI public API ------------------------------------------------------------------


def credentials(env_file: Path) -> tuple[str, str]:
    values = dict(os.environ)
    if not values.get("SENTINELAI_EMAIL") and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() in {"SENTINELAI_EMAIL", "SENTINELAI_PASSWORD"}:
                values.setdefault(key.strip(), value.strip().strip("'\""))
    email, password = values.get("SENTINELAI_EMAIL"), values.get("SENTINELAI_PASSWORD")
    if not email or not password:
        raise SystemExit("SENTINELAI_EMAIL / SENTINELAI_PASSWORD not set (env or repo .env)")
    return email, password


class SentinelApi:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def login(self, email: str, password: str) -> None:
        resp = self.client.post("/api/v1/auth/login", json={"email": email, "password": password})
        if resp.status_code != 200:
            raise SystemExit(f"SentinelAI login failed: HTTP {resp.status_code}")
        self.client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"

    def get(self, path: str, **params: Any) -> Any:
        resp = self.client.get(path, params={k: v for k, v in params.items() if v is not None})
        resp.raise_for_status()
        return resp.json()

    def pages(self, path: str, **params: Any) -> Iterator[dict[str, Any]]:
        cursor = None
        while True:
            page = self.get(path, limit=100, cursor=cursor, **params)
            yield from page["items"]
            cursor = page["next_cursor"]
            if not cursor:
                return

    def events(self, path: str) -> Iterator[dict[str, Any]]:
        """An incident's timeline; this endpoint pages by `after_id` (the last event id)."""
        after_id = None
        while True:
            page = self.get(path, limit=100, after_id=after_id)
            yield from page["items"]
            after_id = page["next_cursor"]
            if not after_id:
                return

    def project(self, slug: str) -> tuple[str, str]:
        for org in self.pages("/api/v1/orgs"):
            for proj in self.pages(f"/api/v1/orgs/{org['id']}/projects"):
                if proj["slug"] == slug:
                    return org["id"], proj["id"]
        raise SystemExit(f"project {slug!r} not found in any of the user's organizations")

    def collect(self, slug: str, start: datetime, end: datetime) -> ApiData:
        org, project = self.project(slug)
        base = f"/api/v1/orgs/{org}"
        services = {
            s["id"]: s["name"]
            for s in self.pages(f"{base}/services", project_id=project, include_archived="true")
        }
        window = {"from": start.isoformat(), "to": end.isoformat()}
        incidents = [
            self.get(f"{base}/incidents/{i['id']}")
            | {"events": list(self.events(f"{base}/incidents/{i['id']}/events"))}
            for i in self.pages(f"{base}/incidents", project_id=project, **window)
        ]
        detections = [
            d for d in self.pages(f"{base}/detections", **window) if d["service_id"] in services
        ]
        return ApiData(services, incidents, detections)


def summary(result: dict[str, Any]) -> str:
    lines = [
        f"{result['scenario']} run {result['run_id']} (harness: {result['run_status']}): "
        f"{'PASS' if result['passed'] else 'FAIL'}"
    ]
    for c in result["checks"]:
        mark = "ok  " if c["passed"] else "FAIL"
        lines.append(f"  [{mark}] {c['check']}: expected {c['expected']}, got {c['actual']}")
    for k, v in result["metrics"].items():
        lines.append(f"  {k}: {v}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="harness.evaluate")
    sub = p.add_subparsers(dest="cmd", required=True)
    inc = sub.add_parser("incidents", help="score SentinelAI incidents for one scenario run")
    inc.add_argument("--run", required=True)
    inc.add_argument("--runs-dir", type=Path, default=_TESTBED_DIR / "runs")
    inc.add_argument("--project", default="shop", help="SentinelAI project slug")
    inc.add_argument("--api", default=os.environ.get("SENTINELAI_API_URL", "http://127.0.0.1:8000"))
    inc.add_argument("--env-file", type=Path, default=_TESTBED_DIR.parent / ".env")
    inc.add_argument(
        "--out", type=Path, help="default: runs/<scenario>/<run>/sentinelai-evaluation.json"
    )
    args = p.parse_args(argv)

    record = find_record(args.runs_dir, args.run)
    if record is None or "run" not in record:
        sys.stderr.write(f"run record {args.run} not found under {args.runs_dir}\n")
        return 2
    if record["timeline"]["started_at"] is None:
        sys.stderr.write(f"run {args.run} has no fault window (fault never started)\n")
        return 2
    with httpx.Client(base_url=args.api, timeout=30) as client:
        api = SentinelApi(client)
        api.login(*credentials(args.env_file))
        data = api.collect(
            args.project,
            ts(record["run"]["baseline_started_at"]),
            ts(record["run"]["recovery_ended_at"]) + timedelta(seconds=1),
        )
    result = evaluate_run(record, data)
    out = args.out or args.runs_dir / record["id"] / args.run / "sentinelai-evaluation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(summary(result) + f"\nwritten: {out}\n")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
