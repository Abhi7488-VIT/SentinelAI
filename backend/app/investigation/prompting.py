"""Builds prompt blocks (invariant 3, plan §7).

Every string that came from telemetry — and every evidence summary/data, which embed such strings
— is placed inside an `<untrusted_telemetry id="ev_…">` block with `&`, `<` and `>` escaped, so
the content can neither close the block nor open a new one. Fields are length-capped. Only
trusted, code-generated text (ids, names from our registry, counts) appears outside blocks, and
registry names are escaped too.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from app.models.investigation import Evidence

BLOCK_SUMMARY_MAX = 1000
BLOCK_DATA_MAX = 4000
TOTAL_EVIDENCE_CHARS = 60_000


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def untrusted(block_id: str, kind: str, content: str) -> str:
    return (
        f'<untrusted_telemetry id="{escape(block_id)}" kind="{escape(kind)}">\n'
        f"{escape(content)}\n"
        "</untrusted_telemetry>"
    )


def evidence_block(ev: Evidence, *, with_data: bool) -> str:
    body = ev.summary[:BLOCK_SUMMARY_MAX]
    if with_data:
        data = json.dumps(ev.data, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(data) > BLOCK_DATA_MAX:
            data = data[:BLOCK_DATA_MAX] + "…(truncated)"
        body += "\nDATA " + data
    return untrusted(ev.id, ev.kind, body)


def evidence_blocks(evidence: Iterable[Evidence], *, with_data: bool) -> tuple[str, list[str]]:
    """All blocks that fit the character budget, and the ids that were left out."""
    parts: list[str] = []
    left_out: list[str] = []
    used = 0
    for ev in evidence:
        block = evidence_block(ev, with_data=with_data)
        if used + len(block) > TOTAL_EVIDENCE_CHARS:
            left_out.append(ev.id)
            continue
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts), left_out


def services_block(title: str, services: Mapping[str, str]) -> str:
    """Trusted list of `id: name` (names come from the registry, still escaped)."""
    lines = [f"- {escape(i)}: {escape(n)}" for i, n in sorted(services.items(), key=lambda x: x[1])]
    return f"{title}:\n" + "\n".join(lines)


def claims_block(findings: Iterable[Mapping[str, Any]]) -> str:
    """Investigator claims for the analyst. They are model output about telemetry, so they are
    wrapped as untrusted too."""
    lines = [
        f"[{f['node']}] {f['statement']} (evidence: {', '.join(f['evidence_ids'])})"
        for f in findings
    ]
    return untrusted("findings", "investigator_findings", "\n".join(lines) or "(none)")
