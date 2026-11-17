"""JSON logging and secret redaction."""

from __future__ import annotations

import json
import logging

import pytest
import structlog

from app.core.logging import REDACTED, configure_logging, get_logger, redact_secrets


def test_redact_secrets_recursive() -> None:
    event = {
        "event": "x",
        "password": "p",
        "Authorization": "Bearer abc",
        "refresh_token": "t",
        "nested": {"api_key": "k", "ok": 1, "deeper": {"client_secret": "s"}},
        "items": [{"cookie": "c"}],
        "user_id": "u1",
    }
    out = redact_secrets(None, "info", event)
    assert out["password"] == REDACTED
    assert out["Authorization"] == REDACTED
    assert out["refresh_token"] == REDACTED
    assert out["nested"] == {"api_key": REDACTED, "ok": 1, "deeper": {"client_secret": REDACTED}}
    assert out["items"] == [{"cookie": REDACTED}]
    assert out["user_id"] == "u1"


def test_logs_are_json_with_context_and_redaction(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    structlog.contextvars.bind_contextvars(request_id="rid-1")
    try:
        get_logger("test").info("user.login", user_id="u1", password="hunter2")
        logging.getLogger("thirdparty").warning("stdlib message")
    finally:
        structlog.contextvars.clear_contextvars()

    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    first, second = lines[-2], lines[-1]
    assert first["event"] == "user.login"
    assert first["request_id"] == "rid-1"
    assert first["password"] == REDACTED
    assert first["level"] == "info"
    assert "timestamp" in first
    assert second["event"] == "stdlib message"
    assert second["logger"] == "thirdparty"
