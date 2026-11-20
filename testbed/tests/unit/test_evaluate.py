"""harness.evaluate: scoring SentinelAI incidents against a run's ground truth (no network)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from harness import evaluate as ev

FAULT_START = "2031-01-01T12:02:00+00:00"
FAULT_END = "2031-01-01T12:07:00+00:00"
RUN_ID = "20310101T120000Z-abc123"
SVC = {
    "s-front": "frontend",
    "s-gw": "api-gateway",
    "s-order": "order-service",
    "s-pay": "payment-service",
    "s-user": "user-service",
}


def record() -> dict[str, Any]:
    return {
        "id": "db-pool-exhaustion",
        "kind": "run",
        "root_cause": {"service": "order-service"},
        "expected_affected_services": ["order-service", "api-gateway", "frontend"],
        "unaffected_services": ["payment-service", "user-service"],
        "timeline": {"started_at": FAULT_START, "ended_at": FAULT_END},
        "run": {
            "run_id": RUN_ID,
            "status": "verified",
            "baseline_started_at": "2031-01-01T12:00:00+00:00",
            "recovery_ended_at": "2031-01-01T12:10:00+00:00",
        },
    }


def detection(
    service_id: str,
    started: str,
    fired: str,
    *,
    rule: str = "error_rate",
    explanation: str | None = None,
) -> dict[str, Any]:
    name = SVC[service_id]
    return {
        "id": f"d-{service_id}-{rule}-{started[11:19]}",
        "service_id": service_id,
        "rule": rule,
        "severity": "warning",
        "metric": "error_ratio",
        "observed_value": 0.5,
        "threshold": 0.05,
        "started_at": started,
        "fired_at": fired,
        "resolved_at": None,
        "explanation": explanation
        or f"{name}: error rate 50.0% (60 of 120 server requests) over 60 s ending 12:03:15Z.",
        "evidence": {"sample_trace_ids": ["ab" * 16]},
    }


def incident(
    number: int,
    started: str,
    dets: list[dict[str, Any]],
    reasons: list[list[dict[str, Any]]],
    *,
    opener: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`opener` (default: the first detection) is named by the `created` event, as the API does."""
    opener = opener or dets[0]
    events = [
        {"id": 10 * number + 1, "type": "created", "payload": {"detection_id": opener["id"]}},
        *(
            {
                "id": 10 * number + 2 + k,
                "type": "detection_added",
                "payload": {"detection_id": d["id"]},
            }
            for k, d in enumerate(dets)
        ),
    ]
    names = sorted({SVC[d["service_id"]] for d in dets})
    return {
        "id": f"i-{number}",
        "number": number,
        "title": "Errors in " + ", ".join(names),
        "status": "open",
        "severity": "warning",
        "started_at": started,
        "detected_at": dets[0]["fired_at"],
        "created_at": dets[0]["fired_at"],
        "services": [{"id": k, "name": v} for k, v in SVC.items() if v in names],
        "detections": [
            {"detection": d, "reasons": r, "score": 3 * len(r)}
            for d, r in zip(dets, reasons, strict=True)
        ],
        "events": events,
    }


SHARED = [{"type": "shared_trace", "detail": "x", "refs": []}]
EDGE = [{"type": "dependency_edge", "detail": "x", "refs": []}]


def good_case() -> ev.ApiData:
    dets = [
        detection("s-order", "2031-01-01T12:02:15+00:00", "2031-01-01T12:03:15+00:00"),
        detection("s-gw", "2031-01-01T12:02:15+00:00", "2031-01-01T12:03:15+00:00"),
        detection("s-front", "2031-01-01T12:02:30+00:00", "2031-01-01T12:03:30+00:00"),
    ]
    inc = incident(1, "2031-01-01T12:02:15+00:00", dets, [[], SHARED + EDGE, SHARED])
    return ev.ApiData(dict(SVC), [inc], dets)


def checks(result: dict[str, Any]) -> dict[str, bool]:
    return {c["check"]: c["passed"] for c in result["checks"]}


def test_a_correct_incident_passes_every_check() -> None:
    result = ev.evaluate_run(record(), good_case())
    assert result["passed"], result["checks"]
    assert result["incident"]["affected_services"] == ["api-gateway", "frontend", "order-service"]
    m = result["metrics"]
    assert m["time_to_first_detection_s"] == 75
    assert m["time_to_incident_s"] == 75
    assert m["detections_in_incident"] == 3
    assert m["false_positive_detections"] == 0
    assert m["root_cause_service_has_detection"] is True
    assert [d["reasons"] for d in result["incident_detections"]][1] == SHARED + EDGE


def test_several_separate_incidents_fail() -> None:
    data = good_case()
    d = data.detections
    split = [
        incident(1, d[0]["started_at"], [d[0]], [[]]),
        incident(2, d[1]["started_at"], [d[1]], [[]]),
        incident(3, d[2]["started_at"], [d[2]], [[]]),
    ]
    result = ev.evaluate_run(record(), ev.ApiData(data.services, split, d))
    assert checks(result)["exactly_one_incident"] is False
    assert result["checks"][0]["actual"] == 3
    assert result["incident"] is None
    assert not result["passed"]


def test_no_incident_fails_without_crashing() -> None:
    result = ev.evaluate_run(record(), ev.ApiData(dict(SVC), [], []))
    assert not result["passed"]
    assert result["metrics"]["time_to_first_detection_s"] is None
    assert checks(result)["detected_during_fault"] is False


def test_unaffected_or_missing_services_fail() -> None:
    data = good_case()
    pay = detection("s-pay", "2031-01-01T12:03:00+00:00", "2031-01-01T12:04:00+00:00")
    with_pay = incident(
        1, data.detections[0]["started_at"], [*data.detections, pay], [[], SHARED, SHARED, EDGE]
    )
    result = ev.evaluate_run(
        record(), ev.ApiData(data.services, [with_pay], [*data.detections, pay])
    )
    c = checks(result)
    assert c["unaffected_services_excluded"] is False
    assert c["no_false_positives"] is False
    assert result["false_positives"][0]["why"] == ["service listed as unaffected"]

    only_order = incident(1, data.detections[0]["started_at"], data.detections[:1], [[]])
    result = ev.evaluate_run(record(), ev.ApiData(data.services, [only_order], data.detections[:1]))
    assert checks(result)["expected_services_affected"] is False


def test_false_positives_outside_the_fault_window() -> None:
    data = good_case()
    before = detection(
        "s-order", "2031-01-01T12:01:00+00:00", "2031-01-01T12:01:45+00:00", rule="latency_shift"
    )
    edge_of_window = detection(
        "s-gw", "2031-01-01T12:07:45+00:00", "2031-01-01T12:08:45+00:00", rule="latency_shift"
    )  # within fault end + 60 s: caused by fault
    late = detection(
        "s-front", "2031-01-01T12:08:30+00:00", "2031-01-01T12:09:30+00:00", rule="latency_shift"
    )
    outside_run = detection("s-front", "2031-01-01T11:00:00+00:00", "2031-01-01T11:01:00+00:00")
    result = ev.evaluate_run(
        record(),
        ev.ApiData(
            data.services,
            data.incidents,
            [*data.detections, before, edge_of_window, late, outside_run],
        ),
    )
    fps = {(f["service"], f["started_at"]): f["why"] for f in result["false_positives"]}
    assert fps == {
        ("order-service", before["started_at"]): ["started outside the fault window"],
        ("frontend", late["started_at"]): ["started outside the fault window"],
    }
    assert result["metrics"]["false_positive_detections"] == 2
    assert not result["passed"]


def test_join_without_a_reason_fails_the_structural_check() -> None:
    data = good_case()
    inc = copy.deepcopy(data.incidents[0])
    inc["detections"][2]["reasons"] = []
    result = ev.evaluate_run(record(), ev.ApiData(data.services, [inc], data.detections))
    assert checks(result)["grouping_is_structural"] is False


def simultaneous_case(opener_reasons: list[dict[str, Any]]) -> tuple[ev.ApiData, list[Any]]:
    """Run 20261119T185934Z-335bba's shape: every detection starts in the same bucket, so the
    API's (started_at, id) order puts the opener (frontend error_rate here) in the middle."""
    at, fired = "2031-01-01T12:02:15+00:00", "2031-01-01T12:03:15+00:00"
    # ids are the live run's UUID prefixes, so the API's tie-break order is the same
    gw = detection("s-gw", at, fired) | {"id": "bc9f21dd"}
    front = detection("s-front", at, fired) | {"id": "510ef464"}
    order = detection("s-order", at, fired, rule="resource_saturation") | {"id": "01819c6b"}
    listed = sorted([gw, front, order], key=lambda d: (d["started_at"], d["id"]))
    assert listed[0] is not front  # the opener is not first, as in the live run
    reasons = [opener_reasons if d is front else SHARED for d in listed]
    inc = incident(1, at, listed, reasons, opener=front)
    return ev.ApiData(dict(SVC), [inc], listed), listed


def test_opener_is_taken_from_the_created_event_when_detections_start_together() -> None:
    data, listed = simultaneous_case(opener_reasons=[])
    result = ev.evaluate_run(record(), data)
    assert result["passed"], result["checks"]
    opener = next(d for d in listed if d["service_id"] == "s-front")
    assert result["opener_detection_id"] == opener["id"]
    structural = next(c for c in result["checks"] if c["check"] == "grouping_is_structural")
    assert opener["id"] not in structural["actual"]
    assert sorted(structural["actual"]) == sorted(d["id"] for d in listed if d is not opener)


def test_a_non_opener_without_reasons_fails_even_when_listed_first() -> None:
    data, listed = simultaneous_case(opener_reasons=[])
    inc = copy.deepcopy(data.incidents[0])
    inc["detections"][0]["reasons"] = []  # listed first, but not the opener
    result = ev.evaluate_run(record(), ev.ApiData(data.services, [inc], data.detections))
    assert checks(result)["grouping_is_structural"] is False
    structural = next(c for c in result["checks"] if c["check"] == "grouping_is_structural")
    assert structural["actual"][listed[0]["id"]] == 0


def test_without_a_created_event_no_detection_is_exempt() -> None:
    data = good_case()
    inc = copy.deepcopy(data.incidents[0])
    inc["events"] = [e for e in inc["events"] if e["type"] != "created"]
    result = ev.evaluate_run(record(), ev.ApiData(data.services, [inc], data.detections))
    assert result["opener_detection_id"] is None
    assert checks(result)["grouping_is_structural"] is False  # the opener's [] now counts


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("order-service: ok", "length"),
        ("api-gateway: error rate is very high for the requests seen in the window", "service"),
        ("order-service: error rate is very high for the requests seen in the window", "number"),
        (
            "order-service: error rate None% (60 of 120 requests) over 60 s ending 12:03Z",
            "leftover",
        ),
        (
            "order-service: error rate {ratio} (60 of 120 requests) over 60 s ending 12:03Z",
            "leftover",
        ),
        (
            "order-service: error rate 50% (60 of 120 requests)\x00 over 60 s ending 12:03Z",
            "printable",
        ),
    ],
)
def test_explanation_problems(text: str, problem: str) -> None:
    found = " ".join(ev.explanation_problems("order-service", text))
    keyword = {
        "service": "start",
        "leftover": "template",
        "printable": "non-printable",
        "number": "number",
        "length": "length",
    }[problem]
    assert keyword in found


def test_unreadable_explanation_fails() -> None:
    data = good_case()
    inc = copy.deepcopy(data.incidents[0])
    inc["detections"][0]["detection"]["explanation"] = "order-service: None"
    result = ev.evaluate_run(record(), ev.ApiData(data.services, [inc], data.detections))
    assert checks(result)["explanations_readable"] is False


# ---- the HTTP side, against a fake SentinelAI ------------------------------------------------


def fake_sentinel(data: ev.ApiData, seen: list[str]) -> httpx.MockTransport:
    def page(items: list[Any], request: httpx.Request) -> httpx.Response:
        q = parse_qs(request.url.query.decode())
        start = int(q.get("cursor", ["0"])[0])
        size = 2  # force pagination
        nxt = str(start + size) if start + size < len(items) else None
        return httpx.Response(200, json={"items": items[start : start + size], "next_cursor": nxt})

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(f"{request.method} {path}")
        if path == "/api/v1/auth/login":
            body = json.loads(request.content)
            ok = body == {"email": "ops@example.com", "password": "pw"}
            return httpx.Response(200 if ok else 401, json={"access_token": "tok"})
        assert request.headers["Authorization"] == "Bearer tok"
        if path == "/api/v1/orgs":
            return page([{"id": "org-x"}, {"id": "org-1"}], request)
        if path == "/api/v1/orgs/org-x/projects":
            return page([{"id": "p-other", "slug": "other"}], request)
        if path == "/api/v1/orgs/org-1/projects":
            return page([{"id": "p-1", "slug": "shop"}], request)
        if path == "/api/v1/orgs/org-1/services":
            assert parse_qs(request.url.query.decode())["project_id"] == ["p-1"]
            return page([{"id": k, "name": v} for k, v in data.services.items()], request)
        if path == "/api/v1/orgs/org-1/incidents":
            q = parse_qs(request.url.query.decode())
            assert {"from", "to", "project_id"} <= set(q)
            return page([{"id": i["id"]} for i in data.incidents], request)
        if path.startswith("/api/v1/orgs/org-1/incidents/") and path.endswith("/events"):
            inc = next(i for i in data.incidents if i["id"] == path.split("/")[-2])
            q = parse_qs(request.url.query.decode())
            after = int(q.get("after_id", ["0"])[0])
            rest = [e for e in inc["events"] if e["id"] > after]
            items = rest[:2]  # force pagination
            nxt = str(items[-1]["id"]) if len(rest) > 2 else None
            return httpx.Response(200, json={"items": items, "next_cursor": nxt})
        if path.startswith("/api/v1/orgs/org-1/incidents/"):
            inc = next(i for i in data.incidents if i["id"] == path.rsplit("/", 1)[1])
            return httpx.Response(200, json={k: v for k, v in inc.items() if k != "events"})
        if path == "/api/v1/orgs/org-1/detections":
            other = {**data.detections[0], "id": "foreign", "service_id": "s-elsewhere"}
            return page([*data.detections, other], request)
        return httpx.Response(404, json={})

    return httpx.MockTransport(handler)


def test_collect_follows_pagination_and_keeps_only_the_projects_services() -> None:
    data = good_case()
    seen: list[str] = []
    with httpx.Client(base_url="http://sentinel", transport=fake_sentinel(data, seen)) as c:
        api = ev.SentinelApi(c)
        api.login("ops@example.com", "pw")
        got = api.collect("shop", ev.ts("2031-01-01T12:00:00Z"), ev.ts("2031-01-01T12:10:00Z"))
    assert got.services == data.services
    assert [i["id"] for i in got.incidents] == ["i-1"]
    assert got.incidents[0]["events"] == data.incidents[0]["events"]  # 4 events over 2 pages
    assert seen.count("GET /api/v1/orgs/org-1/incidents/i-1/events") == 2
    assert [d["id"] for d in got.detections] == [d["id"] for d in data.detections]
    assert seen.count("GET /api/v1/orgs/org-1/services") == 3  # 5 services, 2 per page


def test_login_failure_is_reported_without_the_password() -> None:
    transport = fake_sentinel(good_case(), [])
    with (
        httpx.Client(base_url="http://sentinel", transport=transport) as c,
        pytest.raises(SystemExit) as exc,
    ):
        ev.SentinelApi(c).login("ops@example.com", "wrong-secret")
    assert "401" in str(exc.value)
    assert "wrong-secret" not in str(exc.value)


def test_main_writes_the_evaluation_and_exits_by_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    runs = tmp_path / "runs"
    (runs / "db-pool-exhaustion").mkdir(parents=True)
    (runs / "db-pool-exhaustion" / f"{RUN_ID}.json").write_text(json.dumps(record()))
    data = good_case()
    transport = fake_sentinel(data, [])
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=transport, **kw))
    monkeypatch.setenv("SENTINELAI_EMAIL", "ops@example.com")
    monkeypatch.setenv("SENTINELAI_PASSWORD", "pw")
    code = ev.main(["incidents", "--run", RUN_ID, "--runs-dir", str(runs), "--api", "http://s"])
    assert code == 0
    out = runs / "db-pool-exhaustion" / RUN_ID / "sentinelai-evaluation.json"
    assert json.loads(out.read_text())["passed"] is True
    assert "PASS" in capsys.readouterr().out

    assert ev.main(["incidents", "--run", "20310101T000000Z-ffffff", "--runs-dir", str(runs)]) == 2


def test_the_backend_never_references_the_evaluator() -> None:
    """ADR 0003, harness side of the guard: SentinelAI code must not import this module."""
    backend = Path(__file__).resolve().parents[3] / "backend" / "app"
    if not backend.is_dir():
        pytest.skip("backend not present (testbed-only checkout)")
    offenders = [
        str(p)
        for p in backend.rglob("*.py")
        if "harness" in p.read_text(encoding="utf-8")
        or "ground_truth" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []
