"""Shared test configuration.

Unit tests need no services. Integration tests (marked `integration`) use real Postgres and
Redis from TEST_DATABASE_URL / TEST_REDIS_URL (env or repo-root .env).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from app.core.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]

# Tests must not depend on the shell. A shell that exports the repo's .env would otherwise change
# Settings (e.g. retention days), and Celery lets CELERY_BROKER_URL / CELERY_RESULT_BACKEND
# override the configured broker, which would send test tasks to the live stack's Redis.
# TEST_* variables are not Settings fields and are kept.
for _field in Settings.model_fields:
    os.environ.pop(_field.upper(), None)
_DOTENV = {k: v for k, v in dotenv_values(REPO_ROOT / ".env").items() if v is not None}

TEST_JWT_SECRET = "test-only-jwt-secret-" + "x" * 40


def env_value(name: str, default: str) -> str:
    return os.environ.get(name) or _DOTENV.get(name) or default


TEST_DATABASE_URL = env_value(
    "TEST_DATABASE_URL", "postgresql+asyncpg://sentinel:sentinel@127.0.0.1:5432/sentinel_test"
)
TEST_REDIS_URL = env_value("TEST_REDIS_URL", "redis://127.0.0.1:6379/15")


def make_settings(**overrides: Any) -> Settings:
    """Settings for tests, independent of the developer's .env (except TEST_* URLs)."""
    values: dict[str, Any] = {
        "env": "test",
        "log_level": "WARNING",
        "database_url": TEST_DATABASE_URL,
        "redis_url": TEST_REDIS_URL,
        "celery_broker_url": TEST_REDIS_URL,
        "celery_result_backend": TEST_REDIS_URL,
        "jwt_secret": TEST_JWT_SECRET,
        "cors_origins": ["http://localhost:3000"],
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)
