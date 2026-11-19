"""Service registry request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    model_validator,
)

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


class TelemetryConfig(BaseModel):
    """Per-service telemetry settings. Detection rule overrides are added in Phase 3 T7."""

    model_config = ConfigDict(extra="forbid")

    # Needed by the memory-saturation rule; without it that rule stays inactive.
    memory_limit_bytes: int | None = Field(default=None, ge=_MIB, le=1024 * 1024 * _MIB)


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
