"""Deterministic pre-reduction (invariant 8): plain Python over bounded query results.

Nothing here calls a model. Inputs come from bounded SQL; outputs are small, ordered
deterministically (ties broken by stable keys), and every telemetry string is length-capped.
Telemetry strings are untrusted data: they are stored as data and escaped when placed in a
prompt (T6); this module never interprets them.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# Per-field caps for untrusted strings kept in evidence.
TEMPLATE_MAX = 300
VALUE_MAX = 200

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"),
        "<uuid>",
    ),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<ip>"),
    (
        re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
        "<ts>",
    ),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<hex>"),
    (re.compile(r"\b[0-9a-fA-F]{16,}\b"), "<hex>"),
    (re.compile(r"'[^']*'"), "'<str>'"),
    (re.compile(r'"[^"]*"'), '"<str>"'),
    (re.compile(r"(?<![A-Za-z_])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"), "<n>"),
    (re.compile(r"\s+"), " "),
]

# Python (`File "x.py", line 3, in f`) and JVM/JS (`at pkg.Class.method(File.java:10)`).
_FRAME_PATTERNS = [
    re.compile(r'File "([^"]+)", line (\d+), in (\S+)'),
    re.compile(r"^\s*at\s+(\S.*)$"),
]


def cap(text: str | None, limit: int = VALUE_MAX) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def normalize_message(text: str | None) -> str:
    """Replace variable parts (ids, numbers, addresses, quoted values) with placeholders."""
    if not text:
        return ""
    out = text[:2000]
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return cap(out.strip(), TEMPLATE_MAX) or ""


def top_frame(stacktrace: str | None) -> str | None:
    """The innermost application frame of a stack trace, as `file:line in func` (Python) or the
    first `at …` line (JVM/JS). None when no frame is recognised."""
    if not stacktrace:
        return None
    lines = stacktrace.splitlines()[:400]
    py_frames = [m for line in lines if (m := _FRAME_PATTERNS[0].search(line))]
    if py_frames:
        f = py_frames[-1]  # Python prints the innermost frame last
        return cap(f"{f.group(1)}:{f.group(2)} in {f.group(3)}")
    for line in lines:
        if m := _FRAME_PATTERNS[1].match(line):
            return cap(m.group(1).strip())
    return None


def pct_change(before: float | None, during: float | None) -> float | None:
    """Percent change, None when undefined (no baseline or zero baseline)."""
    if before is None or during is None or before == 0:
        return None
    return round((during - before) / abs(before) * 100.0, 1)


def rnd(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(float(value), digits)


def iso(t: datetime | None) -> str | None:
    return None if t is None else t.isoformat()


# --- logs -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LogSample:
    record_id: str
    ts: datetime
    severity_number: int
    severity_text: str | None
    body: str | None
    exception_type: str | None
    exception_message: str | None
    trace_id: str | None
    attributes: dict[str, Any] = field(default_factory=dict)


def log_template(rec: LogSample) -> str:
    parts = []
    if rec.exception_type:
        parts.append(rec.exception_type.strip())
    text = rec.body or rec.exception_message or ""
    if text:
        parts.append(normalize_message(text))
    return cap(": ".join(parts), TEMPLATE_MAX) or "<empty>"


def cluster_logs(records: Sequence[LogSample], *, top: int = 20) -> list[dict[str, Any]]:
    """Group records by normalised template; largest clusters first (ties by template)."""
    groups: dict[str, list[LogSample]] = {}
    for rec in records:
        groups.setdefault(log_template(rec), []).append(rec)
    clusters: list[dict[str, Any]] = []
    for template, recs in groups.items():
        recs_sorted = sorted(recs, key=lambda r: (r.ts, r.record_id))
        attrs: Counter[tuple[str, str]] = Counter()
        for r in recs:
            for k, v in list(r.attributes.items())[:50]:
                if isinstance(v, str | int | float | bool):
                    attrs[(cap(str(k), 80) or "", cap(str(v), 80) or "")] += 1
        severities = Counter(r.severity_text or str(r.severity_number) for r in recs)
        clusters.append(
            {
                "template": template,
                "count": len(recs),
                "severity": sorted(severities.items(), key=lambda kv: (-kv[1], kv[0]))[0][0],
                "first_seen": iso(recs_sorted[0].ts),
                "last_seen": iso(recs_sorted[-1].ts),
                # Most recent three records, by id (resolvable through the evidence's query).
                "example_ids": [r.record_id for r in reversed(recs_sorted[-3:])],
                "example_trace_ids": sorted({r.trace_id for r in recs if r.trace_id})[:3],
                "top_attributes": [
                    {"key": k, "value": v, "count": n}
                    for (k, v), n in sorted(attrs.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
                ],
            }
        )
    clusters.sort(key=lambda c: (-int(c["count"]), str(c["template"])))
    return clusters[:top]


# --- traces -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpanNode:
    span_id: str
    parent_span_id: str | None
    service: str
    name: str
    start: datetime
    end: datetime
    duration_ms: float
    error: bool


def _union_ms(intervals: Iterable[tuple[datetime, datetime]], lo: datetime, hi: datetime) -> float:
    """Total length (ms) of the union of intervals clipped to [lo, hi]."""
    clipped = sorted((max(a, lo), min(b, hi)) for a, b in intervals if b > lo and a < hi)
    total = 0.0
    cur_a: datetime | None = None
    cur_b: datetime | None = None
    for a, b in clipped:
        if cur_b is None or a > cur_b:
            if cur_a is not None and cur_b is not None:
                total += (cur_b - cur_a).total_seconds() * 1000
            cur_a, cur_b = a, b
        elif b > cur_b:
            cur_b = b
    if cur_a is not None and cur_b is not None:
        total += (cur_b - cur_a).total_seconds() * 1000
    return total


def critical_path(spans: Sequence[SpanNode], *, max_depth: int = 25) -> list[dict[str, Any]]:
    """Chain from the root through the child that finishes last, at each level.

    Self-time = the span's duration minus the time covered by its children. A trace with several
    roots (missing parents) starts from the longest root. Cycles and depth are bounded.
    """
    if not spans:
        return []
    by_id = {s.span_id: s for s in spans}
    children: dict[str, list[SpanNode]] = {}
    roots = []
    for s in spans:
        if s.parent_span_id and s.parent_span_id in by_id and s.parent_span_id != s.span_id:
            children.setdefault(s.parent_span_id, []).append(s)
        else:
            roots.append(s)
    if not roots:
        return []
    node: SpanNode | None = sorted(roots, key=lambda s: (-s.duration_ms, s.start, s.span_id))[0]
    path: list[dict[str, Any]] = []
    seen: set[str] = set()
    while node is not None and node.span_id not in seen and len(path) < max_depth:
        seen.add(node.span_id)
        kids = children.get(node.span_id, [])
        covered = _union_ms(((k.start, k.end) for k in kids), node.start, node.end)
        path.append(
            {
                "service": node.service,
                "operation": cap(node.name, 120),
                "duration_ms": rnd(node.duration_ms, 1),
                "self_ms": rnd(max(node.duration_ms - covered, 0.0), 1),
                "error": node.error,
            }
        )
        node = (
            sorted(kids, key=lambda k: (k.end, k.duration_ms, k.span_id), reverse=True)[0]
            if kids
            else None
        )
    return path


# --- size budget ------------------------------------------------------------------------------


def json_size(data: Any) -> int:
    """Bytes of `data` as JSON with jsonb-like separators (UTF-8, unescaped non-ASCII)."""
    return len(json.dumps(data, ensure_ascii=False, separators=(", ", ": "), default=str).encode())


def fit_to_budget(data: dict[str, Any], budget: int) -> tuple[dict[str, Any], bool]:
    """Drop trailing items from the longest top-level lists until `data` fits `budget` bytes.

    Lists are ordered most-important-first by every tool, so trimming the tail keeps the top-N.
    Raises ValueError if nothing is left to trim and it still does not fit.
    """
    out = dict(data)
    truncated = False
    while json_size(out) > budget:
        lists = [(len(v), k) for k, v in out.items() if isinstance(v, list) and v]
        if not lists:
            raise ValueError("evidence data exceeds its size budget and has nothing to trim")
        _, key = max(lists)
        items = out[key]
        out[key] = items[: len(items) // 2 if len(items) > 8 else len(items) - 1]
        truncated = True
    return out, truncated
