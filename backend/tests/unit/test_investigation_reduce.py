"""Deterministic pre-reduction (Phase 4 T2): templates, clusters, frames, critical path, budget."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from app.investigation.evidence import dedup_key, new_evidence_id
from app.investigation.reduce import (
    LogSample,
    SpanNode,
    cluster_logs,
    critical_path,
    fit_to_budget,
    json_size,
    normalize_message,
    pct_change,
    top_frame,
)
from app.models.investigation import EVIDENCE_ID_PATTERN

T0 = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def test_normalize_replaces_variable_parts() -> None:
    a = normalize_message("timeout after 30.5s for order 4711 from 10.0.0.12:5432 id 'abc'")
    b = normalize_message("timeout after 12s for order 99 from 10.1.2.3:6543 id 'zzz'")
    assert a == b == "timeout after <n>s for order <n> from <ip> id '<str>'"
    uid = "9f1c2d3e-1111-2222-3333-444455556666"
    assert normalize_message(f"user {uid} missing") == "user <uuid> missing"
    assert normalize_message("trace 4bf92f3577b34da6a3ce929d0e0e4736") == "trace <hex>"
    assert normalize_message(None) == ""
    assert len(normalize_message("x " * 5000)) <= 300


def test_top_frame_python_innermost_and_jvm_first() -> None:
    py = (
        "Traceback (most recent call last):\n"
        '  File "/app/api.py", line 10, in handler\n'
        '  File "/app/db.py", line 42, in checkout\n'
        "TimeoutError: pool"
    )
    assert top_frame(py) == "/app/db.py:42 in checkout"
    jvm = "java.lang.X: boom\n\tat com.shop.Pool.get(Pool.java:88)\n\tat com.shop.Api.h(Api.java:1)"
    assert top_frame(jvm) == "com.shop.Pool.get(Pool.java:88)"
    assert top_frame("no frames here") is None
    assert top_frame(None) is None


def _log(i: int, body: str, *, sev: int = 17, attrs: dict[str, object] | None = None) -> LogSample:
    return LogSample(
        record_id=f"{i:064x}",
        ts=T0 + timedelta(seconds=i),
        severity_number=sev,
        severity_text="ERROR",
        body=body,
        exception_type=None,
        exception_message=None,
        trace_id=f"{i:032x}",
        attributes=attrs or {},
    )


def test_cluster_logs_groups_orders_and_keeps_recent_examples() -> None:
    recs = [_log(i, f"pool timeout after {i}ms", attrs={"pool": "orders"}) for i in range(5)]
    recs += [_log(10 + i, f"cache miss key {i}") for i in range(2)]
    clusters = cluster_logs(recs)
    assert [c["template"] for c in clusters] == ["pool timeout after <n>ms", "cache miss key <n>"]
    top = clusters[0]
    assert top["count"] == 5
    assert top["first_seen"] == (T0).isoformat()
    assert top["last_seen"] == (T0 + timedelta(seconds=4)).isoformat()
    assert top["example_ids"] == [f"{i:064x}" for i in (4, 3, 2)]
    assert top["top_attributes"] == [{"key": "pool", "value": "orders", "count": 5}]
    # Same input, same output (deterministic, order-independent).
    assert cluster_logs(list(reversed(recs))) == clusters


def test_cluster_logs_caps_number_of_clusters() -> None:
    recs = [_log(i, f"distinct-{chr(65 + i % 26)}{chr(65 + i // 26)} failure") for i in range(60)]
    assert len(cluster_logs(recs, top=20)) == 20


def _span(sid: str, parent: str | None, svc: str, start_ms: int, end_ms: int) -> SpanNode:
    return SpanNode(
        span_id=sid,
        parent_span_id=parent,
        service=svc,
        name=f"op-{sid}",
        start=T0 + timedelta(milliseconds=start_ms),
        end=T0 + timedelta(milliseconds=end_ms),
        duration_ms=float(end_ms - start_ms),
        error=False,
    )


def test_critical_path_follows_last_finishing_child_with_self_time() -> None:
    spans = [
        _span("a", None, "gateway", 0, 1000),
        _span("b", "a", "orders", 10, 200),
        _span("c", "a", "orders", 210, 990),  # finishes last
        _span("d", "c", "postgres", 220, 900),
    ]
    path = critical_path(spans)
    assert [p["operation"] for p in path] == ["op-a", "op-c", "op-d"]
    assert path[0]["self_ms"] == pytest.approx(1000 - 190 - 780)
    assert path[1]["self_ms"] == pytest.approx(780 - 680)
    assert path[2]["self_ms"] == pytest.approx(680)


def test_critical_path_overlapping_children_are_not_double_counted() -> None:
    spans = [
        _span("a", None, "s", 0, 100),
        _span("b", "a", "s", 0, 60),
        _span("c", "a", "s", 40, 80),
    ]
    assert critical_path(spans)[0]["self_ms"] == pytest.approx(20)


def test_critical_path_handles_cycles_and_missing_parents() -> None:
    spans = [_span("x", "y", "s", 0, 10), _span("y", "x", "s", 0, 5)]
    assert critical_path(spans) == []  # no root at all
    orphan = [_span("o", "missing", "s", 0, 50)]
    assert critical_path(orphan)[0]["operation"] == "op-o"


def test_fit_to_budget_trims_longest_list_tail_and_flags_truncation() -> None:
    data = {"name": "x", "clusters": [{"t": "y" * 100, "i": i} for i in range(400)], "few": [1]}
    out, truncated = fit_to_budget(data, 5_000)
    assert truncated
    assert json_size(out) <= 5_000
    assert out["clusters"] == data["clusters"][: len(out["clusters"])]  # kept the head
    assert out["few"] == [1]
    same, flag = fit_to_budget({"a": [1, 2]}, 5_000)
    assert same == {"a": [1, 2]}
    assert not flag
    with pytest.raises(ValueError, match="nothing to trim"):
        fit_to_budget({"blob": "z" * 10_000}, 5_000)


def test_pct_change() -> None:
    assert pct_change(10, 15) == 50.0
    assert pct_change(0, 5) is None
    assert pct_change(None, 5) is None
    assert pct_change(-4, -2) == 50.0


def test_evidence_ids_and_dedup_keys() -> None:
    ids = {new_evidence_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(re.fullmatch(EVIDENCE_ID_PATTERN, i) for i in ids)
    k1 = dedup_key("log_query", {"a": 1, "b": [2, 3]})
    assert k1 == dedup_key("log_query", {"b": [2, 3], "a": 1})
    assert k1 != dedup_key("metric_query", {"a": 1, "b": [2, 3]})
    assert len(k1) == 64
