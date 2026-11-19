# ADR 0002 — Tenant isolation: application-layer scoping in one place; RLS deferred

- Status: **Accepted**
- Date: 2026-11-17
- Phase: 1 (Foundation)

## Context

CLAUDE.md invariant 4: every tenant-owned table has `org_id`, every repository query filters by
it, enforcement lives in one place, and tests prove cross-org access returns 404. SentinelAI will
store sensitive operational data (telemetry, code, incidents), so a missed filter is a data leak.

## Decision

1. **Membership gate (API layer).** Every `/api/v1/orgs/{org_id}/...` route depends on
   `require_org_role(min_role)` (`app/api/tenancy.py`). It loads the caller's membership for the
   path `org_id`:
   - not a member, or the org doesn't exist → **404 `org_not_found`** (existence is not leaked);
   - member with insufficient role → **403 `forbidden`**;
   - otherwise it returns a `TenantContext(org_id, user_id, role)`.
2. **Scoped repositories (data layer).** Tenant-owned models are only queried through
   `TenantRepository` subclasses (`app/repositories/base.py`), which require a `TenantContext`.
   Every SELECT starts from `scoped()` (`WHERE org_id = ctx.org_id`); `add()` stamps `org_id` from
   the context and ignores any client value; `delete()` refuses foreign rows. An object id from
   another org therefore resolves to "not found" → 404.
3. **Roles are never cached in tokens**; they are read on every request (ADR 0001).
4. **Guard tests** keep this true as the code grows:
   - every org-scoped endpoint in the OpenAPI schema returns 404 to a non-member
     (`tests/integration/test_tenant_isolation.py`);
   - every ORM model with an `org_id` column has a `TenantRepository`
     (`tests/unit/test_architecture.py`);
   - `app/services` and `app/api` may not import SQL builders (`select`, `update`, `delete`,
     `insert`, `text`) — all queries live in repositories.
5. **Future child tables** carry `org_id` too and reference parents through composite foreign keys
   `(org_id, project_id) → projects(org_id, id)` (the unique constraint already exists), so a row
   cannot point across tenants even through a bug.

### Documented exceptions (not tenant-scoped, and why)
- `MembershipRepository` / `OrganizationRepository`: they *establish* tenancy. Queries filter by
  the authenticated user's own id (`organization_members.user_id`) or by an org id whose membership
  was just verified.
- `find_key_by_prefix` (`app/repositories/ingest_keys.py`, Phase 3): OTLP ingest authentication.
  There is no TenantContext before the key is verified — the key *is* the tenant credential. The
  lookup is by the key's public prefix and returns only what authentication needs; the org and
  service of every ingested record are then taken from the verified key, never from the payload.
- `AuditLogWriter`: insert-only; user-level events (login) legitimately have `org_id = NULL`.
  Reads go through the scoped `AuditLogRepository`.

## Alternatives considered

- **Postgres Row-Level Security** (policies on `current_setting('app.org_id')`). Strong defense in
  depth, but it needs per-transaction `SET LOCAL` on pooled connections (including Celery workers
  and Alembic), complicates superuser/migration roles, and is easy to misconfigure silently.
  **Deferred to Phase 6 hardening** as a second layer, not a replacement.
- **Schema- or database-per-tenant.** Strong isolation but heavy operationally for an MVP and
  awkward for cross-tenant evaluation tooling. Rejected for now.

## Consequences

- Isolation depends on code discipline, which the guard tests enforce mechanically.
- One indexed primary-key lookup per org-scoped request for the membership check.
