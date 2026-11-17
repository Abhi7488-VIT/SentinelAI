"""Settings validation guards."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.conftest import make_settings


def test_valid_test_settings() -> None:
    settings = make_settings()
    assert settings.env == "test"
    assert "x" * 10 not in repr(settings)  # secrets masked in repr


def test_short_jwt_secret_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        make_settings(jwt_secret="too-short")


def test_prod_requires_secure_cookie() -> None:
    with pytest.raises(ValidationError, match="COOKIE_SECURE"):
        make_settings(env="prod", cookie_secure=False)


def test_prod_rejects_placeholder_secret() -> None:
    with pytest.raises(ValidationError, match="placeholder"):
        make_settings(
            env="prod",
            cookie_secure=True,
            jwt_secret="change-me-to-a-long-random-string-of-at-least-32-bytes",
        )


def test_family_ttl_must_cover_token_ttl() -> None:
    with pytest.raises(ValidationError, match="REFRESH_FAMILY_TTL_SECONDS"):
        make_settings(refresh_token_ttl_seconds=7200, refresh_family_ttl_seconds=3600)


def test_cors_origins_parsed_from_json_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import Settings

    monkeypatch.setenv("CORS_ORIGINS", '["http://a.example", "http://b.example"]')
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("REDIS_URL", "redis://h/0")
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://h/1")
    monkeypatch.setenv("CELERY_RESULT_BACKEND", "redis://h/2")
    monkeypatch.setenv("JWT_SECRET", "y" * 40)
    settings = Settings(_env_file=None)
    assert settings.cors_origins == ["http://a.example", "http://b.example"]
