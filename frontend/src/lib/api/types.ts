import type { components } from "./schema";

type Schemas = components["schemas"];

export type User = Schemas["UserOut"];
export type Me = Schemas["MeResponse"];
export type Membership = Schemas["MembershipOut"];
export type Role = Schemas["Role"];
export type OrgWithRole = Schemas["OrgWithRoleOut"];
export type Project = Schemas["ProjectOut"];
export type ProjectCreate = Schemas["ProjectCreate"];
export type ProjectPage = Schemas["Page_ProjectOut_"];

const ROLE_RANK: Record<Role, number> = { viewer: 0, engineer: 1, admin: 2, owner: 3 };

export function roleAtLeast(role: Role | undefined, min: Role): boolean {
  return role !== undefined && ROLE_RANK[role] >= ROLE_RANK[min];
}
