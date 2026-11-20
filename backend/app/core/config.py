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

    # OTLP ingestion (Phase 3 plan §4). Sizes in bytes; rates per key per second.
    ingest_max_body_bytes: int = Field(default=4 * 1024 * 1024, ge=1024)
    ingest_max_decompressed_bytes: int = Field(default=16 * 1024 * 1024, ge=1024)
    ingest_max_records_per_request: int = Field(default=10_000, ge=1)
    ingest_rate_requests_per_second: int = Field(default=50, ge=1)
    ingest_rate_records_per_second: int = Field(default=20_000, ge=1)
    ingest_max_age_seconds: int = Field(default=7200, ge=60)
    ingest_max_future_seconds: int = Field(default=300, ge=0)
    ingest_key_cache_seconds: float = Field(default=10.0, ge=0, le=60)
    ingest_queue_max_pending: int = Field(default=100, ge=1)
    ingest_queue_max_message_bytes: int = Field(default=2 * 1024 * 1024, ge=1024)
    # Writer: metric cardinality guard and capped dead-letter list.
    ingest_max_series_per_service: int = Field(default=2000, ge=1)
    ingest_dead_letter_max: int = Field(default=1000, ge=1)

    # LLM provider (Phase 4 plan §3). The key is read from the environment only (A4); never
    # logged, stored, or put into events, reports or errors.
    llm_provider: Literal["fake", "anthropic"] = "fake"
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-sonnet-5-5"  # development default (A1); opus-5-5 for final eval
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    llm_timeout_seconds: float = Field(default=90.0, gt=0, le=600)
    llm_max_invalid_retries: int = Field(default=2, ge=0, le=5)
    llm_max_transport_retries: int = Field(default=3, ge=0, le=6)
    llm_max_input_tokens: int = Field(default=30_000, ge=1000, le=200_000)
    llm_max_output_tokens: int = Field(default=8000, ge=256, le=64_000)
    llm_prices_json: str | None = None  # override of app.llm.pricing.DEFAULT_PRICES

    # HTTP
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    @field_validator("jwt_secret")
    @classmethod
    def _jwt_secret_long_enough(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode()) < MIN_JWT_SECRET_BYTES:
            raise ValueError(f"JWT_SECRET must be at least {MIN_JWT_SECRET_BYTES} bytes")
        return value

    @field_validator("anthropic_api_key", "llm_prices_json", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        # `ANTHROPIC_API_KEY=` in .env means "not configured", not an empty key.
        return None if value == "" else value

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
