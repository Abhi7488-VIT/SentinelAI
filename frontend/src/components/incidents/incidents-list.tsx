/* Incidents table. Presentational; all four states share one frame. */
import Link from "next/link";

import { SeverityPill, StatusPill } from "@/components/common/pills";
import { EmptyBlock, ErrorBlock, type ErrorInfo, SkeletonBars } from "@/components/common/states";
import type { Incident } from "@/lib/api/types";
import { formatDateTime, formatDuration } from "@/lib/format";

export type IncidentsState =
  | { kind: "loading" }
  | { kind: "error"; error: ErrorInfo }
  | { kind: "ready"; incidents: Incident[]; filtered: boolean };

const GRID =
  "md:grid md:grid-cols-[64px_minmax(0,2.4fr)_96px_112px_minmax(0,1fr)_96px] md:items-center md:gap-4";

export function IncidentsList({
  state,
  orgId,
  onRetry,
  now,
}: {
  state: IncidentsState;
  orgId: string;
  onRetry: () => void;
  now?: number;
}) {
  if (state.kind === "loading")
    return (
      <section aria-label="Incidents" className="px-4 md:px-6">
        <SkeletonBars label="incidents" />
      </section>
    );
  if (state.kind === "error")
    return (
      <section aria-label="Incidents" className="px-4 md:px-6">
        <ErrorBlock what="incidents" error={state.error} onRetry={onRetry} />
      </section>
    );
  if (state.incidents.length === 0)
    return (
      <section aria-label="Incidents" className="px-4 md:px-6">
        <EmptyBlock>
          {state.filtered
            ? "No incidents match these filters."
            : "No incidents. They open automatically when detections fire on services sending telemetry."}
        </EmptyBlock>
      </section>
    );
  return (
    <section aria-label="Incidents">
      <div role="presentation" className={`hidden h-9 px-6 text-xs text-muted-foreground ${GRID}`}>
        <span>#</span>
        <span>Incident</span>
        <span>Severity</span>
        <span>Status</span>
        <span>Started (UTC)</span>
        <span className="text-right">Duration</span>
      </div>
      <ul className="grid border-b border-rule">
        {state.incidents.map((i) => (
          <li key={i.id} className={`grid gap-1.5 border-t border-rule px-4 py-3 md:min-h-14 md:px-6 md:py-2 ${GRID}`}>
            <span className="num font-mono text-sm text-muted-foreground">#{i.number}</span>
            <div className="min-w-0">
              <Link href={`/orgs/${orgId}/incidents/${i.id}`} className="block truncate text-sm font-medium hover:underline">
                {i.title}
              </Link>
              <p className="truncate text-xs text-muted-foreground">{i.services.map((s) => s.name).join(", ")}</p>
            </div>
            <div className="flex gap-2 md:contents">
              <span>
                <SeverityPill severity={i.severity} />
              </span>
              <span>
                <StatusPill status={i.status} />
              </span>
            </div>
            <span className="num text-sm text-muted-foreground">{formatDateTime(i.started_at)}</span>
            <span className="num text-sm text-muted-foreground md:text-right">
              {formatDuration(i.started_at, i.resolved_at, now)}
              {i.resolved_at ? "" : " so far"}
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
