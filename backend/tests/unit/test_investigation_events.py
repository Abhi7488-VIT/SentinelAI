"""Event payload rules, report citation gate, SSE framing (Phase 4 T7)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.api.v1.investigations import InvalidLastEventIdError, _resume_point
from app.investigation.events import ALLOWED_FIELDS, EventPayloadError, build_payload, secret_like
from app.investigation.report import citation_gate, cited_ids
from app.models.investigation import EVENT_PAYLOAD_MAX_BYTES, EVENT_TYPES, InvestigationEvent
from app.services.investigation_events import sse_frame


def test_every_event_type_has_an_allow_list_without_credential_keys() -> None:
    assert set(ALLOWED_FIELDS) == set(EVENT_TYPES)
    for fields in ALLOWED_FIELDS.values():
        assert not [f for f in fields if secret_like(f)]


@pytest.mark.parametrize(
    "key", ["api_key", "token", "access_token", "password", "authorization", "secret", "Cookie"]
)
def test_credential_like_keys_are_detected(key: str) -> None:
    assert secret_like(key)


def test_counts_are_not_mistaken_for_credentials() -> None:
    assert not secret_like("input_tokens")
    assert not secret_like("output_tokens")


def test_payload_rejects_unknown_types_and_fields() -> None:
    with pytest.raises(EventPayloadError, match="unknown event type"):
        build_payload("chat_message", {})
    with pytest.raises(EventPayloadError, match="not allowed"):
        build_payload("llm_call", {"purpose": "analyst", "prompt": "the whole prompt"})
    with pytest.raises(EventPayloadError, match="not allowed"):
        build_payload("stage_started", {"stage": "x", "authorization": "Bearer abc"})


def test_payload_values_are_capped() -> None:
    out = build_payload(
        "tool_completed",
        {"evidence_id": "ev_abcdefghijkl", "summary": "x" * 5000, "tool": "y" * 999},
    )
    assert len(out["summary"]) == 1000
    assert len(out["tool"]) == 200
    assert len(json.dumps(out).encode()) <= EVENT_PAYLOAD_MAX_BYTES
    cost = build_payload("llm_call", {"cost_usd": Decimal("0.012345"), "input_tokens": 10})
    assert cost == {"cost_usd": "0.012345", "input_tokens": 10}


def test_citation_gate_finds_ids_anywhere() -> None:
    report = {
        "summary": {"text": "see ev_aaaaaaaaaaaa and ev_bbbbbbbbbbbb"},
        "nested": [{"evidence_id": "ev_cccccccccccc"}],
    }
    assert cited_ids(report) == ["ev_aaaaaaaaaaaa", "ev_bbbbbbbbbbbb", "ev_cccccccccccc"]
    assert citation_gate(report, {"ev_aaaaaaaaaaaa", "ev_cccccccccccc"}) == ["ev_bbbbbbbbbbbb"]
    assert citation_gate(report, set(cited_ids(report))) == []


def test_sse_frame_format() -> None:
    ev = InvestigationEvent(
        id=42,
        type="stage_started",
        payload={"stage": "planner"},
        created_at=datetime(2026, 11, 23, tzinfo=UTC),
    )
    frame = sse_frame(ev)
    assert frame.startswith("id: 42\nevent: stage_started\ndata: ")
    assert frame.endswith("\n\n")
    data = json.loads(frame.split("data: ", 1)[1])
    assert data == {
        "id": 42,
        "type": "stage_started",
        "at": "2026-11-23T00:00:00+00:00",
        "stage": "planner",
    }


def test_resume_point() -> None:
    assert _resume_point(None, None) is None
    assert _resume_point(None, 7) == 7
    assert _resume_point("12", 7) == 12  # the header wins
    for bad in ("-1", "abc", "1.5", "9" * 25):
        with pytest.raises(InvalidLastEventIdError):
            _resume_point(bad, None)
