from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.models.organization import SLUG_PATTERN


class Project(TimestampMixin, Base):
    """Tenant-owned. Accessed only through a TenantRepository (filters by org_id)."""

    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name="slug_format"),
        UniqueConstraint("org_id", "slug", name="uq_projects_org_id_slug"),
        # Target for future composite FKs (org_id, project_id) -> projects(org_id, id)
        # so child rows can never reference a project in another tenant.
        UniqueConstraint("org_id", "id", name="uq_projects_org_id_id"),
        # Keyset pagination ORDER BY created_at DESC, id DESC uses a backward index scan.
        Index("ix_projects_org_id_created_at_id", "org_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(63), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
