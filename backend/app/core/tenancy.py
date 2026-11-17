"""Tenant context and role policy (pure logic, no I/O)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.models.organization import Role


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Proof that `user_id` is a member of `org_id` with `role`.

    Produced only by the `require_org_role` API dependency after a membership lookup;
    TenantRepository instances require one, which is how every tenant query is scoped.
    """

    org_id: uuid.UUID
    user_id: uuid.UUID
    role: Role


def can_assign_role(actor: Role, target_current: Role | None, target_new: Role | None) -> bool:
    """Whether `actor` may change a member from `target_current` to `target_new`.

    None means "not a member" (adding) / "removed" (removing).
    Owners may do anything. Admins may only manage engineer/viewer memberships.
    Engineers and viewers manage nothing.
    """
    if actor is Role.OWNER:
        return True
    if actor is Role.ADMIN:
        managed = {Role.ENGINEER, Role.VIEWER}
        return all(r is None or r in managed for r in (target_current, target_new))
    return False
