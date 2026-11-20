"""Service registry request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    model_validator,
)

from app.detection.rules import Rule, params_for
from app.models.service import (
    ENVIRONMENT_PATTERN,
    EXTERNAL_TARGET_PATTERN,
    SERVICE_NAME_PATTERN,
    HealthStatus,
)
from app.schemas.auth import Name
from app.schemas.common import APIModel
from app.schemas.projects import Description

ServiceName = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=128, pattern=SERVICE_NAME_PATTERN)
]
Environment = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=63, pattern=ENVIRONMENT_PATTERN)
]
ExternalTarget = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=255, pattern=EXTERNAL_TARGET_PATTERN)
]
OverridableRule = Literal["error_rate", "latency_shift", "resource_saturation", "health_check"]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]

_MIB = 1024 * 1024


def _no_credentials(url: HttpUrl) -> HttpUrl:
    # Secrets never go into the registry (CLAUDE.md: secrets in .env only).
    if url.username or url.password:
        raise ValueError("repository URL must not contain credentials")
    return url


RepoUrl = Annotated[HttpUrl, Field(max_length=2048), AfterValidator(_no_credentials)]


class ServiceDeployment(BaseModel):
    """Free-form but bounded description of how the service is deployed."""

    model_config = ConfigDict(extra="forbid")

    platform: ShortText | None = None  # e.g. "docker-compose", "kubernetes"
    runtime: ShortText | None = None  # e.g. "python3.12"
    region: ShortText | None = None
    current_version: ShortText | None = None


class RuleOverride(BaseModel):
    """Override of one detection rule's defaults (ADR 0006). Unset fields keep the default."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    fire: float | None = Field(default=None, gt=0, le=1_000_000)
    clear: float | None = Field(default=None, ge=0, le=1_000_000)
    critical: float | None = Field(default=None, gt=0, le=1_000_000)
    min_samples: int | None = Field(default=None, ge=1, le=100_000)
    min_duration_s: int | None = Field(default=None, ge=0, le=3600)
    clear_duration_s: int | None = Field(default=None, ge=0, le=3600)


class TelemetryConfig(BaseModel):
    """Per-service telemetry settings."""

    model_config = ConfigDict(extra="forbid")

    # Needed by the memory-saturation rule; without it that rule stays inactive.
    memory_limit_bytes: int | None = Field(default=None, ge=_MIB, le=1024 * 1024 * _MIB)
    # Detection rule overrides, by rule name (deploy_change has no tunable thresholds).
    detection: dict[OverridableRule, RuleOverride] | None = None

    @model_validator(mode="after")
    def _hysteresis(self) -> TelemetryConfig:
        for rule, override in (self.detection or {}).items():
            merged = params_for(Rule(rule), override.model_dump(exclude_none=True))
            if merged.clear >= merged.fire:
                raise ValueError(f"detection.{rule}: clear must be below fire")
            if merged.critical is not None and merged.critical < merged.fire:
                raise ValueError(f"detection.{rule}: critical must be at or above fire")
        return self


class ServiceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: uuid.UUID
    name: ServiceName
    environment: Environment
    description: Description | None = None
    repo_url: RepoUrl | None = None
    deployment: ServiceDeployment = Field(default_factory=ServiceDeployment)
    telemetry_config: TelemetryConfig = Field(default_factory=TelemetryConfig)


_NON_NULLABLE_UPDATE_FIELDS = ("name", "environment", "deployment", "telemetry_config")
_UPDATE_FIELDS = (*_NON_NULLABLE_UPDATE_FIELDS, "description", "repo_url")


class ServiceUpdate(BaseModel):
    """Partial update. `project_id` cannot change; `description`/`repo_url` may be null."""

    model_config = ConfigDict(extra="forbid")

    name: ServiceName | None = None
    environment: Environment | None = None
    description: Description | None = None
    repo_url: RepoUrl | None = None
    deployment: ServiceDeployment | None = None
    telemetry_config: TelemetryConfig | None = None

    @model_validator(mode="after")
    def _required_not_null(self) -> ServiceUpdate:
        for field in _NON_NULLABLE_UPDATE_FIELDS:
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self

    def changes(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for field in _UPDATE_FIELDS:
            if field not in self.model_fields_set:
                continue
            value = getattr(self, field)
            if isinstance(value, BaseModel):
                value = value.model_dump(exclude_none=True)
            elif isinstance(value, HttpUrl):
                value = str(value)
            out[field] = value
        return out


class DependencyIn(BaseModel):
    """Exactly one of `target_service_id` (same project) or `target_external`."""

    model_config = ConfigDict(extra="forbid")

    target_service_id: uuid.UUID | None = None
    target_external: ExternalTarget | None = None
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = (
        None
    )

    @model_validator(mode="after")
    def _one_target(self) -> DependencyIn:
        if (self.target_service_id is None) == (self.target_external is None):
            raise ValueError("set exactly one of target_service_id or target_external")
        return self


class DependenciesPut(BaseModel):
    """Replaces the full set of declared dependencies of a service."""

    model_config = ConfigDict(extra="forbid")

    dependencies: list[DependencyIn] = Field(max_length=100)


class DependencyOut(APIModel):
    id: uuid.UUID
    target_service_id: uuid.UUID | None
    target_external: str | None
    description: str | None
    created_at: datetime


class ServiceOut(APIModel):
    id: uuid.UUID
    org_id: uuid.UUID
    project_id: uuid.UUID
    name: str
    environment: str
    description: str | None
    repo_url: str | None
    deployment: ServiceDeployment
    telemetry_config: TelemetryConfig
    health_status: HealthStatus
    last_seen_at: datetime | None
    archived_at: datetime | None
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    dependencies: list[DependencyOut]


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name


class ApiKeyOut(APIModel):
    id: uuid.UUID
    service_id: uuid.UUID
    name: str
    prefix: str
    created_by: uuid.UUID | None
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    revoked_by: uuid.UUID | None


class ApiKeyCreated(ApiKeyOut):
    """Returned once, at creation. `key` is never retrievable again."""

    key: str
