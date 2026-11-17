from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from app.schemas.auth import Name
from app.schemas.common import APIModel
from app.schemas.orgs import Slug

Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    slug: Slug | None = None
    description: Description | None = None


class ProjectUpdate(BaseModel):
    """Partial update: only fields present in the request body are changed.

    `description: null` clears the description; `name` cannot be null.
    """

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    description: Description | None = None

    @model_validator(mode="after")
    def _name_not_null(self) -> ProjectUpdate:
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name cannot be null")
        return self

    def changes(self) -> dict[str, str | None]:
        return {k: getattr(self, k) for k in ("name", "description") if k in self.model_fields_set}


class ProjectOut(APIModel):
    id: uuid.UUID
    org_id: uuid.UUID
    name: str
    slug: str
    description: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
