"""Deterministic verification (Phase 4 T4): citations, category checks, status, ranking."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.investigation.taxonomy import TAXONOMY
from app.investigation.verification import CHECKS, EvidenceView, Verdict, rank, verify

T = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def iso(minutes: float) -> str:
    return (T + timedelta(minutes=minutes)).isoformat()


def evidence(*, gateway_first: bool = False, deploy_min: float | None = None) -> list[EvidenceView]:
    orders_start, gw_start = (2, 1) if gateway_first else (1, 2)
    deployments = (
        [
            {
                "service": "orders",
                "version": "2.0",
                "at": iso(-deploy_min),
                "minutes_before_onset": deploy_min,
            }
        ]
        if deploy_min is not None
        else []
    )
    return [
        EvidenceView(
            "ev_detections0",
            "detection_ref",
            {
                "detections": [
                    {"service": "orders", "rule": "error_rate", "started_at": iso(orders_start)},
                    {"service": "gateway", "rule": "error_rate", "started_at": iso(gw_start)},
                ]
            },
        ),
        EvidenceView(
            "ev_logsorders0",
            "log_query",
            {
                "service": "orders",
                "clusters": [
                    {
                        "template": "TimeoutError: connection pool exhausted after <n>ms",
                        "count": 30,
                    },
                ],
            },
        ),
        EvidenceView(
            "ev_logsgateway",
            "log_query",
            {
                "service": "gateway",
                "clusters": [
                    {"template": "upstream returned <n>", "count": 9},
                ],
            },
        ),
        EvidenceView(
            "ev_tracesall00",
            "trace_analysis",
            {
                "operations": [
                    {
                        "service": "orders",
                        "operation": "SELECT orders",
                        "kind": "client",
                        "error_pct_change_pp": 90.0,
                        "p95_change_pct": 15900.0,
                        "baseline": {"spans": 120},
                        "incident": {"spans": 120},
                    },
                    {
                        "service": "orders",
                        "operation": "POST /orders",
                        "kind": "server",
                        "error_pct_change_pp": 90.0,
                        "p95_change_pct": 4150.0,
                        "baseline": {"spans": 120},
                        "incident": {"spans": 120},
                    },
                ],
                "exception_signatures": [],
                "critical_paths": [
                    {
                        "reason": "slowest",
                        "path": [
                            {
                                "service": "gateway",
                                "operation": "POST /orders",
                                "self_ms": 40,
                                "error": True,
                            },
                            {
                                "service": "orders",
                                "operation": "SELECT orders",
                                "self_ms": 800,
                                "error": True,
                            },
                        ],
                    },
                    {
                        "reason": "erroring",
                        "path": [
                            {
                                "service": "gateway",
                                "operation": "POST /orders",
                                "self_ms": 40,
                                "error": True,
                            },
                            {
                                "service": "orders",
                                "operation": "SELECT orders",
                                "self_ms": 800,
                                "error": True,
                            },
                        ],
                    },
                ],
            },
        ),
        EvidenceView("ev_deploys0000", "deploy_lookup", {"deployments": deployments}),
        EvidenceView(
            "ev_edges000000",
            "dependency_lookup",
            {
                "edges": [
                    {
                        "source": "gateway",
                        "target": "orders",
                        "stats": {"error_pct_change_pp": 90.0, "calls_change_pct": 0.0},
                    },
                ]
            },
        ),
    ]


def v(category: str, subject: str, ev: list[EvidenceView], **kw: Any) -> Verdict:
    return verify(
        category=category,
        subject_names={subject},
        supporting=kw.get("supporting", ["ev_logsorders0", "ev_tracesall00"]),
        contradicting=kw.get("contradicting", []),
        evidence=ev,
    )


def test_every_taxonomy_check_is_implemented() -> None:
    names = {c for spec in TAXONOMY.values() for c in spec.checks}
    assert names == set(CHECKS)


def test_all_checks_pass_on_direct_evidence_confirms() -> None:
    out = v("resource_exhaustion", "orders", evidence())
    assert [c.result for c in out.checks] == ["pass", "pass", "pass"]
    assert out.status == "confirmed"
    for check in out.checks:  # each result names the stored evidence it looked at
        assert check.evidence_ids
        assert set(check.evidence_ids) <= {e.id for e in evidence()}


def test_contradicting_evidence_prevents_confirmed() -> None:
    out = v("resource_exhaustion", "orders", evidence(), contradicting=["ev_logsgateway"])
    assert out.passed == 3
    assert out.status == "probable"
    assert out.contradicting == ("ev_logsgateway",)


def test_reversed_onset_order_fails_a_check_so_not_confirmed() -> None:
    out = v("resource_exhaustion", "orders", evidence(gateway_first=True))
    by = {c.name: c.result for c in out.checks}
    assert by["subject_onset_not_after_first_downstream_symptom"] == "fail"
    assert out.status == "probable"


def test_citations_alone_with_no_passing_check_are_inconclusive() -> None:
    """A confident model citing real evidence about an unrelated service gains nothing:
    confidence is not even an input to verify()."""
    out = v("resource_exhaustion", "payments", evidence())
    assert out.passed == 0
    assert out.status == "inconclusive"


def test_unknown_citations_are_rejected_and_none_left_is_inconclusive() -> None:
    out = v(
        "resource_exhaustion",
        "orders",
        evidence(),
        supporting=["ev_logsorders0", "ev_madeup00000"],
        contradicting=["ev_fakefake000"],
    )
    assert out.supporting == ("ev_logsorders0",)
    assert out.rejected == ("ev_fakefake000", "ev_madeup00000")
    assert out.status == "confirmed"  # the unknown contradicting id does not count either
    only_fake = v("resource_exhaustion", "orders", evidence(), supporting=["ev_madeup00000"])
    assert only_fake.status == "inconclusive"
    assert only_fake.reason == "no valid supporting citation"


def test_other_category_cannot_be_confirmed() -> None:
    out = v("other", "orders", evidence())
    assert out.checks == ()
    assert out.status == "inconclusive"


def test_memory_leak_is_never_confirmed_without_series_slope() -> None:
    out = v("memory_or_resource_leak", "orders", evidence())
    by = {c.name: c.result for c in out.checks}
    assert by == {
        "monotonic_increase_before_onset": "not_run",
        "restarts_or_oom_if_present": "not_applicable",
    }
    assert out.status == "inconclusive"


@pytest.mark.parametrize(
    ("deploy_min", "expected"), [(6.0, "confirmed"), (90.0, "probable"), (None, "inconclusive")]
)
def test_bad_deployment(deploy_min: float | None, expected: str) -> None:
    out = v("bad_deployment", "orders", evidence(deploy_min=deploy_min))
    assert out.status == expected


def test_slow_query_and_dependency_checks() -> None:
    slow = v("slow_dependency_or_query", "orders", evidence())
    assert slow.status == "confirmed"
    wrong = v("slow_dependency_or_query", "gateway", evidence())
    by = {c.name: c.result for c in wrong.checks}
    assert by["operation_self_time_dominates_critical_path_increase"] == "fail"
    assert wrong.status != "confirmed"
    dep = v("dependency_unavailable", "orders", evidence())
    assert [c.result for c in dep.checks] == ["pass", "pass", "pass"]
    up = v("upstream_propagation", "gateway", evidence())
    assert up.checks[0].result == "pass"


def test_ranking_is_deterministic_and_ignores_model_confidence() -> None:
    ev = evidence()
    verdicts = [
        (0, v("other", "orders", ev)),  # inconclusive
        (1, v("resource_exhaustion", "orders", ev, contradicting=["ev_logsgateway"])),  # probable
        (2, v("resource_exhaustion", "orders", ev)),  # confirmed
        (3, v("resource_exhaustion", "payments", ev)),  # inconclusive
    ]
    expected = [2, 1, 0, 3]
    assert rank(verdicts) == expected
    for _ in range(10):
        shuffled = verdicts[:]
        random.shuffle(shuffled)
        assert rank(shuffled) == expected
