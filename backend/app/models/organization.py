from __future__ import annotations

import enum
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

# Lowercase alnum + hyphens, no leading/trailing hyphen, 1-63 chars (DNS-label-like).
SLUG_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"


class Role(enum.StrEnum):
    VIEWER = "viewer"
    ENGINEER = "engineer"
    ADMIN = "admin"
    OWNER = "owner"

    @property
    def rank(self) -> int:
        return _ROLE_RANK[self]

    def at_least(self, other: Role) -> bool:
        return self.rank >= other.rank


_ROLE_RANK: dict[Role, int] = {Role.VIEWER: 0, Role.ENGINEER: 1, Role.ADMIN: 2, Role.OWNER: 3}

_ROLE_VALUES = ", ".join(f"'{r.value}'" for r in Role)


class Organization(TimestampMixin, Base):
    """The tenant root. Every tenant-owned row references an organization via org_id."""

    __tablename__ = "organizations"
    __table_args__ = (CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name="slug_format"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)


class OrganizationMember(TimestampMixin, Base):
    __tablename__ = "organization_members"
    __table_args__ = (
        CheckConstraint(f"role IN ({_ROLE_VALUES})", name="role_valid"),
        Index("ix_organization_members_user_id", "user_id"),
    )

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
