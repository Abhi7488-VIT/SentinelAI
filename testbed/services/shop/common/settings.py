"""Settings shared by every shop service, read from the environment."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

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

    # Dependencies (only the ones a service uses are set in its container).
    database_url: SecretStr | None = None
    redis_url: SecretStr | None = None
    gateway_url: str | None = None
    user_service_url: str | None = None
    order_service_url: str | None = None
    payment_service_url: str | None = None

    # Telemetry (wired in T3). Unset = telemetry export disabled.
    otel_exporter_otlp_endpoint: str | None = None

    @classmethod
    def for_service(cls, name: ServiceName) -> Self:
        """The service's identity comes from code; everything else from the environment."""
        return cls(service_name=name)
