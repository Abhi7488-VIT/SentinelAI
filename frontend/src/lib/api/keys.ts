/** TanStack Query keys, one place so mutations invalidate exactly what they change. */
export const keys = {
  project: (orgId: string, projectId: string) => ["project", orgId, projectId] as const,
  services: (orgId: string, projectId: string) => ["services", orgId, projectId] as const,
  apiKeys: (orgId: string, serviceId: string) => ["api-keys", orgId, serviceId] as const,
  overview: (orgId: string, projectId: string) => ["overview", orgId, projectId] as const,
  graph: (orgId: string, projectId: string) => ["service-graph", orgId, projectId] as const,
  incidents: (orgId: string, filters: Record<string, string>) => ["incidents", orgId, filters] as const,
  incident: (orgId: string, incidentId: string) => ["incident", orgId, incidentId] as const,
  incidentEvents: (orgId: string, incidentId: string) => ["incident-events", orgId, incidentId] as const,
};

/** Read views poll at the detection tick's pace (SSE arrives in Phase 4). */
export const POLL_MS = 15_000;
