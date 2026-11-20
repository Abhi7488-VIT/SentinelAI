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

export type HealthStatus = Schemas["HealthStatus"];
export type Service = Schemas["ServiceOut"];
export type ServiceCreate = Schemas["ServiceCreate"];
export type Dependency = Schemas["DependencyOut"];
export type DependencyIn = Schemas["DependencyIn"];
export type ApiKey = Schemas["ApiKeyOut"];
export type ApiKeyCreated = Schemas["ApiKeyCreated"];
export type Incident = Schemas["IncidentOut"];
export type IncidentDetail = Schemas["IncidentDetailOut"];
export type IncidentDetection = Schemas["IncidentDetectionOut"];
export type IncidentEvent = Schemas["IncidentEventOut"];
export type IncidentStatus = Incident["status"];
export type Severity = Incident["severity"];
export type GroupingReason = Schemas["GroupingReason"];
export type Detection = Schemas["DetectionOut"];
export type GraphNode = Schemas["GraphNode"];
export type GraphEdge = Schemas["GraphEdge"];
export type ServiceGraph = Schemas["ServiceGraphOut"];
export type Overview = Schemas["OverviewOut"];
export type OverviewService = Schemas["OverviewServiceOut"];
export type IngestUsageMinute = Schemas["IngestUsageMinuteOut"];
