"use client";

/*
 * Incidents list with filters kept in the URL (status, severity, project, service, time range,
 * search), so a filtered view can be shared or reloaded. Polls every 15 s.
 */
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

import { errorInfo } from "@/components/common/states";
import { type IncidentsState, IncidentsList } from "@/components/incidents/incidents-list";
import { useOrg } from "@/components/shell/org-context";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, unwrap } from "@/lib/api/client";
import { keys, POLL_MS } from "@/lib/api/keys";
import type { IncidentStatus, Severity } from "@/lib/api/types";

const STATUSES: IncidentStatus[] = ["open", "investigating", "mitigated", "resolved"];
const SEVERITIES: Severity[] = ["critical", "warning"];
export const RANGES = { "1h": 3_600_000, "24h": 86_400_000, "7d": 604_800_000 } as const;
type Range = keyof typeof RANGES;

const FILTER_KEYS = ["status", "severity", "project", "service", "range", "q"] as const;
type Filters = Partial<Record<(typeof FILTER_KEYS)[number], string>>;

export function readFilters(params: URLSearchParams): Filters {
  const f: Filters = {};
  for (const k of FILTER_KEYS) {
    const v = params.get(k);
    if (v) f[k] = v;
  }
  return f;
}

/** URL filters → API query; invalid values are dropped rather than sent. */
export function toQuery(f: Filters, now: number) {
  return {
    status: STATUSES.includes(f.status as IncidentStatus) ? (f.status as IncidentStatus) : undefined,
    severity: SEVERITIES.includes(f.severity as Severity) ? (f.severity as Severity) : undefined,
    project_id: f.project,
    service_id: f.project ? f.service : undefined,
    from: f.range && f.range in RANGES ? new Date(now - RANGES[f.range as Range]).toISOString() : undefined,
    q: f.q?.trim() || undefined,
  };
}

const SELECT =
  "h-9 rounded-md border border-input bg-background px-2 text-sm focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none";

export function IncidentsView() {
  const { orgId } = useOrg();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const filters = readFilters(new URLSearchParams(params.toString()));
  const [search, setSearch] = useState(filters.q ?? "");

  const set = (patch: Filters) => {
    const next = new URLSearchParams(params.toString());
    for (const [k, v] of Object.entries(patch)) {
      if (v) next.set(k, v);
      else next.delete(k);
    }
    if ("project" in patch) next.delete("service");
    const qs = next.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
  };

  const projects = useQuery({
    queryKey: ["projects-all", orgId],
    queryFn: () =>
      unwrap(api.GET("/api/v1/orgs/{org_id}/projects", { params: { path: { org_id: orgId }, query: { limit: 100 } } })),
  });
  const services = useQuery({
    queryKey: keys.services(orgId, filters.project ?? ""),
    enabled: Boolean(filters.project),
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/orgs/{org_id}/services", {
          params: { path: { org_id: orgId }, query: { project_id: filters.project, limit: 100 } },
        }),
      ),
  });

  const query = useInfiniteQuery({
    queryKey: keys.incidents(orgId, filters as Record<string, string>),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/api/v1/orgs/{org_id}/incidents", {
          params: { path: { org_id: orgId }, query: { ...toQuery(filters, Date.now()), limit: 50, cursor: pageParam } },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: POLL_MS,
  });

  const filtered = Object.keys(filters).length > 0;
  const state: IncidentsState = query.isPending
    ? { kind: "loading" }
    : query.isError
      ? { kind: "error", error: errorInfo(query.error) }
      : { kind: "ready", incidents: query.data.pages.flatMap((p) => p.items), filtered };

  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-5 pt-6 pb-10">
      <div className="grid gap-1 px-4 md:px-6">
        <h1 className="text-[22px] leading-7 font-semibold">Incidents</h1>
        <p className="text-sm text-muted-foreground">
          Grouped from detections by shared traces, exception signatures, dependency edges and deploys. Updated every
          15 s.
        </p>
      </div>
      <form
        role="search"
        aria-label="Filter incidents"
        className="flex flex-wrap items-end gap-2 px-4 md:px-6"
        onSubmit={(e) => {
          e.preventDefault();
          set({ q: search.trim() || undefined });
        }}
      >
        <Input
          aria-label="Search title or #number"
          placeholder="Search title or #number"
          className="h-9 w-56"
          value={search}
          maxLength={200}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select aria-label="Status" className={SELECT} value={filters.status ?? ""} onChange={(e) => set({ status: e.target.value })}>
          <option value="">Any status</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s[0].toUpperCase() + s.slice(1)}
            </option>
          ))}
        </select>
        <select aria-label="Severity" className={SELECT} value={filters.severity ?? ""} onChange={(e) => set({ severity: e.target.value })}>
          <option value="">Any severity</option>
          {SEVERITIES.map((s) => (
            <option key={s} value={s}>
              {s[0].toUpperCase() + s.slice(1)}
            </option>
          ))}
        </select>
        <select aria-label="Project" className={SELECT} value={filters.project ?? ""} onChange={(e) => set({ project: e.target.value })}>
          <option value="">All projects</option>
          {projects.data?.items.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        {filters.project ? (
          <select aria-label="Service" className={SELECT} value={filters.service ?? ""} onChange={(e) => set({ service: e.target.value })}>
            <option value="">All services</option>
            {services.data?.items.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        ) : null}
        <select aria-label="Started within" className={SELECT} value={filters.range ?? ""} onChange={(e) => set({ range: e.target.value })}>
          <option value="">Any time</option>
          <option value="1h">Last hour</option>
          <option value="24h">Last 24 hours</option>
          <option value="7d">Last 7 days</option>
        </select>
        <Button type="submit" variant="outline" className="h-9">
          Search
        </Button>
        {filtered ? (
          <Button
            type="button"
            variant="ghost"
            className="h-9"
            onClick={() => {
              setSearch("");
              router.replace(pathname, { scroll: false });
            }}
          >
            Clear filters
          </Button>
        ) : null}
      </form>
      <IncidentsList state={state} orgId={orgId} onRetry={() => void query.refetch()} />
      {query.hasNextPage ? (
        <div className="px-4 md:px-6">
          <Button variant="outline" className="h-8" onClick={() => void query.fetchNextPage()} disabled={query.isFetchingNextPage}>
            {query.isFetchingNextPage ? "Loading…" : "Load more"}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
