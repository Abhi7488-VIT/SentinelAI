from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

from app.core.slugs import MAX_SLUG_LENGTH, SLUG_REGEX
from app.models.organization import Role
from app.schemas.auth import Name
from app.schemas.common import APIModel

Slug = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, max_length=MAX_SLUG_LENGTH, pattern=SLUG_REGEX.pattern
    ),
]


class OrgCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    slug: Slug | None = None


class OrgUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name | None = None


class OrgOut(APIModel):
    id: uuid.UUID
    name: str
    slug: str
    created_at: datetime
    updated_at: datetime


class OrgWithRoleOut(OrgOut):
    role: Role


class MemberAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    role: Role


class MemberUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role


class MemberOut(APIModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    role: Role
    created_at: datetime


class AuditLogOut(APIModel):
    id: int
    action: str
    actor_user_id: uuid.UUID | None
    target_type: str | None
    target_id: str | None
    ip: str | None
    user_agent: str | None
    request_id: str | None
    metadata: dict[str, Any] = Field(validation_alias="metadata_")
    created_at: datetime
