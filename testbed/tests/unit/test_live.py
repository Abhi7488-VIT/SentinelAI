"""Pure parts of the live-check runner (the checks themselves run in Codespaces/CI)."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from harness.live import (
    SERVICES,
    baseline_verdict,
    fmt,
    last_json_line,
    main,
    prom_rows,
    render_summary,
    window_delta,
)

ALL = set(SERVICES)


def _totals(requests: int, errors: int, declined: int, **by_action: int) -> dict[str, object]:
    return {"requests": requests, "errors": errors, "declined": declined, "by_action": by_action}


def test_window_delta() -> None:
    before = _totals(100, 1, 2, browse=60, checkout=40)
    after = _totals(6100, 4, 50, browse=3660, checkout=2440)
    delta = window_delta(before, after)
    assert delta["requests"] == 6000
    assert delta["errors"] == 3
    assert delta["declined"] == 48
    assert delta["error_rate"] == pytest.approx(0.0005)
    assert delta["by_action"] == {"browse": 3600, "checkout": 2400}


def test_window_delta_with_no_traffic() -> None:
    same = _totals(10, 0, 0, browse=10)
    assert window_delta(same, same)["error_rate"] is None


def test_baseline_passes_when_errors_low_and_all_services_present() -> None:
    delta = {"requests": 6000, "error_rate": 0.0005}
    ok, reasons = baseline_verdict(delta, {"prometheus": ALL, "jaeger": ALL, "loki": ALL})
    assert ok
    assert reasons == []


def test_baseline_fails_on_error_rate_and_missing_services() -> None:
    delta = {"requests": 6000, "error_rate": 0.012}
    presence = {"prometheus": ALL, "jaeger": ALL - {"payment-service"}, "loki": ALL}
    ok, reasons = baseline_verdict(delta, presence)
    assert not ok
    assert any("error rate 1.200%" in r for r in reasons)
    assert any("jaeger is missing services: payment-service" in r for r in reasons)


def test_baseline_fails_without_traffic() -> None:
    ok, reasons = baseline_verdict(
        {"requests": 0, "error_rate": None}, {"prometheus": ALL, "jaeger": ALL, "loki": ALL}
    )
    assert not ok
    assert reasons == ["no requests were recorded in the window"]


def test_prom_rows_groups_by_service() -> None:
    rows = prom_rows(
        {
            "req/s": [{"metric": {"service_name": "frontend"}, "value": [0, "9.5"]}],
            "p95 ms": [{"metric": {"service_name": "frontend"}, "value": [0, "120.0"]}],
        }
    )
    assert rows == {"frontend": {"req/s": 9.5, "p95 ms": 120.0}}


def test_last_json_line_ignores_noise() -> None:
    output = 'Container x Started\n{"status": "succeeded", "seconds": 7.1}\n'
    assert last_json_line(output) == {"status": "succeeded", "seconds": 7.1}
    with pytest.raises(ValueError, match="no JSON"):
        last_json_line("nothing useful")


@pytest.mark.parametrize(
    ("value", "column", "expected"),
    [
        (9.512, "req/s", "9.51"),
        (0.0004, "5xx share", "0.04%"),
        (135.84, "p95 ms", "135.8"),
        (math.nan, "p95 ms", "-"),
        (None, "req/s", "-"),
        (3.0, "pool timeouts", "3"),
    ],
)
def test_fmt(value: float | None, column: str, expected: str) -> None:
    assert fmt(value, column) == expected


def _baseline_record(passed: bool) -> dict[str, object]:
    return {
        "check": "baseline",
        "run_id": "r1",
        "started_at": "t0",
        "ended_at": "t1",
        "seed": 42,
        "minutes": 10,
        "passed": passed,
        "reasons": [] if passed else ["error rate 1.2% >= 0.5%"],
        "window": {
            "requests": 6000,
            "requests_per_s": 10.0,
            "errors": 3,
            "error_rate": 0.0005,
            "declined": 48,
        },
        "prometheus": {
            "frontend": {
                "req/s": 9.5,
                "5xx share": 0.0,
                "p50 ms": 14.0,
                "p95 ms": 135.0,
                "pool timeouts": math.nan,
            }
        },
        "presence": {"prometheus": sorted(ALL), "jaeger": sorted(ALL), "loki": sorted(ALL)},
        "evidence_files": 12,
    }


def test_render_baseline_summary() -> None:
    text = render_summary(_baseline_record(True))
    assert "**PASS**" in text
    assert "| 10 min | 6000 | 10.00 | 3 | 0.050% | 48 |" in text
    assert "| frontend | 9.50 | 0.00% | 14.0 | 135.0 | - |" in text
    assert "12 query outputs" in text
    failed = render_summary(_baseline_record(False))
    assert "**FAIL**" in failed
    assert "- error rate 1.2% >= 0.5%" in failed


def test_render_deploy_summary() -> None:
    record = {
        "check": "deploy-check",
        "run_id": "r2",
        "started_at": "t0",
        "ended_at": "t1",
        "passed": True,
        "reasons": [],
        "evidence_files": 9,
        "steps": [{"name": "deploy payment-service 1.2.1", "result": "ok — status succeeded"}],
    }
    text = render_summary(record)
    assert "| deploy payment-service 1.2.1 | ok — status succeeded |" in text


@pytest.mark.parametrize(
    "argv",
    [["1"], ["5"], ["chaos"], ["baseline", "--minutes", "0"], ["baseline", "--minutes", "31"]],
)
def test_cli_refuses_unavailable_or_invalid_checks(argv: list[str], tmp_path: Path) -> None:
    assert main([*argv, "--out", str(tmp_path)]) == 2
    assert not any(tmp_path.iterdir())  # nothing ran, nothing written


def test_trace_services_lists_every_service_in_a_trace() -> None:
    from harness.live import trace_services

    trace = {
        "processes": {"p1": {"serviceName": "frontend"}, "p2": {"serviceName": "order-service"}},
        "spans": [{"processID": "p2"}, {"processID": "p1"}, {"processID": "p2"}],
    }
    assert trace_services(trace) == ["frontend", "order-service"]
    assert trace_services({}) == []
