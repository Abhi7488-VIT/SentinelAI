"""Application settings, loaded from environment variables (and `.env` files in dev).

Secrets are `SecretStr` so they never appear in reprs or logs.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_JWT_SECRET_BYTES = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Later files take priority: backend/.env overrides repo-root .env.
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # PostgreSQL
    database_url: SecretStr
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_statement_timeout_ms: int = Field(default=5000, ge=100, le=600_000)

    # Redis / Celery
    redis_url: SecretStr
    celery_broker_url: SecretStr
    celery_result_backend: SecretStr

    # Auth
    jwt_secret: SecretStr
    jwt_issuer: str = "sentinelai"
    jwt_audience: str = "sentinelai-api"
    access_token_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    refresh_token_ttl_seconds: int = Field(default=1_209_600, ge=3600)
    refresh_family_ttl_seconds: int = Field(default=2_592_000, ge=3600)
    cookie_secure: bool = False
    auth_rate_limit_attempts: int = Field(default=10, ge=1)
    auth_rate_limit_window_seconds: int = Field(default=60, ge=1)

    # Telemetry storage (ADR 0005). Daily partitions; retention in whole days.
    telemetry_retention_spans_days: int = Field(default=3, ge=1, le=90)
    telemetry_retention_logs_days: int = Field(default=3, ge=1, le=90)
    telemetry_retention_metrics_days: int = Field(default=7, ge=1, le=90)
    telemetry_partition_days_ahead: int = Field(default=2, ge=1, le=14)
    ingest_usage_retention_days: int = Field(default=30, ge=1, le=365)

    # HTTP
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    @field_validator("jwt_secret")
    @classmethod
    def _jwt_secret_long_enough(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode()) < MIN_JWT_SECRET_BYTES:
            raise ValueError(f"JWT_SECRET must be at least {MIN_JWT_SECRET_BYTES} bytes")
        return value

    @model_validator(mode="after")
    def _prod_guards(self) -> Settings:
        if self.env == "prod":
            if not self.cookie_secure:
                raise ValueError("COOKIE_SECURE must be true when ENV=prod")
            if self.jwt_secret.get_secret_value().startswith("change-me"):
                raise ValueError("JWT_SECRET still has the placeholder value")
        if self.refresh_family_ttl_seconds < self.refresh_token_ttl_seconds:
            raise ValueError("REFRESH_FAMILY_TTL_SECONDS must be >= REFRESH_TOKEN_TTL_SECONDS")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # values come from the environment
