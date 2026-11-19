"""Settings shared by every shop service, read from the environment and the active release."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Self

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from shop.common.releases import active_release

ServiceName = Literal[
    "frontend",
    "api-gateway",
    "user-service",
    "order-service",
    "payment-service",
]

SERVICE_NAMES: tuple[ServiceName, ...] = (
    "frontend",
    "api-gateway",
    "user-service",
    "order-service",
    "payment-service",
)


class ServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    service_name: ServiceName
    service_version: str = "0.0.0"
    environment: str = "testbed"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Database (services that own one). database_name, when set, replaces the URL's database.
    database_url: SecretStr | None = None
    database_name: str | None = None
    db_pool_size: int = 5
    db_max_overflow: int = 0
    db_pool_timeout_s: float = 3.0
    db_statement_timeout_ms: int = 10_000

    redis_url: SecretStr | None = None

    # Downstream services
    gateway_url: str | None = None
    user_service_url: str | None = None
    order_service_url: str | None = None
    payment_service_url: str | None = None
    http_timeout_s: float = 5.0

    # Deterministic behaviour (payment issuer decisions, etc.)
    seed: int = 42

    # Releases manifest written by the deploy pipeline (absent = use env SERVICE_VERSION)
    releases_file: Path = Path("/releases/releases.json")

    # Token the deploy pipeline presents to POST /ops/restart. Unset = endpoint disabled.
    ops_token: SecretStr | None = None

    # Telemetry (wired in T3). Unset = telemetry export disabled.
    otel_exporter_otlp_endpoint: str | None = None

    @classmethod
    def for_service(cls, name: ServiceName, **overrides: Any) -> Self:
        """Identity comes from code; the active release may set version and config."""
        base = cls(service_name=name, **overrides)
        release = active_release(base.releases_file, name)
        if release is None:
            return base
        values: dict[str, Any] = {"service_version": release.version}
        values.update({k.lower(): v for k, v in release.config.items()})
        values.update(overrides)
        return cls(service_name=name, **values)

    def database_dsn(self) -> str:
        if self.database_url is None:
            raise RuntimeError(f"{self.service_name} has no DATABASE_URL")
        dsn = self.database_url.get_secret_value()
        if self.database_name:
            dsn = dsn.rsplit("/", 1)[0] + "/" + self.database_name
        return dsn
