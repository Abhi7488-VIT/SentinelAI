from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, StringConstraints

from app.core.security.passwords import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH
from app.models.organization import Role
from app.schemas.common import APIModel

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
NewPassword = Annotated[
    str, StringConstraints(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
]
AnyPassword = Annotated[str, StringConstraints(min_length=1, max_length=MAX_PASSWORD_LENGTH)]


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: NewPassword
    display_name: Name
    org_name: Name


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: AnyPassword


class UserOut(APIModel):
    id: uuid.UUID
    email: str
    display_name: str
    created_at: datetime


class OrganizationOut(APIModel):
    id: uuid.UUID
    name: str
    slug: str
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105  (OAuth token type, not a secret)
    expires_in: int


class RegisterResponse(TokenResponse):
    user: UserOut
    organization: OrganizationOut
    membership_role: Role


class LoginResponse(TokenResponse):
    user: UserOut


class MembershipOut(BaseModel):
    org_id: uuid.UUID
    org_name: str
    org_slug: str
    role: Role


class MeResponse(BaseModel):
    user: UserOut
    memberships: list[MembershipOut]
