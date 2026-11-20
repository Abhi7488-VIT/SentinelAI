/*
 * The services of a project. Presentational: one component renders loading / error / empty / data
 * so the states share a frame. Actions are passed in and shown only for roles allowed to use them.
 */
import { HealthPill } from "@/components/common/pills";
import { EmptyBlock, ErrorBlock, type ErrorInfo, SkeletonBars } from "@/components/common/states";
import { Button } from "@/components/ui/button";
import type { Service } from "@/lib/api/types";
import { formatAgo } from "@/lib/format";

export type ServicesState =
  | { kind: "loading" }
  | { kind: "error"; error: ErrorInfo }
  | { kind: "ready"; services: Service[] };

const GRID =
  "md:grid md:grid-cols-[minmax(0,1.6fr)_minmax(0,0.8fr)_minmax(0,0.9fr)_minmax(0,1.6fr)_auto] md:items-center md:gap-6";

function dependencyNames(service: Service, byId: Map<string, Service>): string {
  if (service.dependencies.length === 0) return "None declared";
  return service.dependencies
    .map((d) => (d.target_service_id ? (byId.get(d.target_service_id)?.name ?? "unknown service") : d.target_external))
    .join(", ");
}

export interface ServiceActions {
  canEdit: boolean;
  canAdmin: boolean;
  onDependencies: (s: Service) => void;
  onKeys: (s: Service) => void;
  onArchive: (s: Service) => void;
}

export function ServicesList({
  state,
  actions,
  onRetry,
  onCreate,
  now,
}: {
  state: ServicesState;
  actions: ServiceActions;
  onRetry: () => void;
  onCreate?: () => void;
  now?: number;
}) {
  if (state.kind === "loading") {
    return (
      <section aria-label="Services" className="px-4 md:px-6">
        <SkeletonBars label="services" />
      </section>
    );
  }
  if (state.kind === "error") {
    return (
      <section aria-label="Services" className="px-4 md:px-6">
        <ErrorBlock what="services" error={state.error} onRetry={onRetry} />
      </section>
    );
  }
  if (state.services.length === 0) {
    return (
      <section aria-label="Services" className="px-4 md:px-6">
        <EmptyBlock
          action={
            onCreate ? (
              <Button variant="link" className="h-auto p-0 text-sm" onClick={onCreate}>
                New service
              </Button>
            ) : undefined
          }
        >
          No services yet. Register a service, then create an ingest key for its OpenTelemetry exporter.
        </EmptyBlock>
      </section>
    );
  }
  const byId = new Map(state.services.map((s) => [s.id, s]));
  return (
    <section aria-label="Services">
      <div role="presentation" className={`hidden h-9 px-6 text-xs text-muted-foreground ${GRID}`}>
        <span>Service</span>
        <span>Health</span>
        <span>Last telemetry</span>
        <span>Depends on</span>
        <span className="sr-only">Actions</span>
      </div>
      <ul className="grid border-b border-rule">
        {state.services.map((s) => (
          <li key={s.id} className={`grid gap-2 border-t border-rule px-4 py-3 md:min-h-14 md:px-6 md:py-2 ${GRID}`}>
            <div className="min-w-0">
              <p className="truncate text-sm font-medium">{s.name}</p>
              <p className="truncate font-mono text-xs text-muted-foreground">{s.environment}</p>
            </div>
            <div>
              <HealthPill status={s.health_status} />
            </div>
            <p className="num text-sm text-muted-foreground" title={s.last_seen_at ?? undefined}>
              {s.last_seen_at ? formatAgo(s.last_seen_at, now) : "No telemetry yet"}
            </p>
            <p className="truncate text-sm text-muted-foreground">{dependencyNames(s, byId)}</p>
            <div className="flex flex-wrap gap-1 md:justify-end">
              {actions.canEdit ? (
                <Button variant="outline" className="h-8" onClick={() => actions.onDependencies(s)}>
                  Dependencies
                </Button>
              ) : null}
              {actions.canAdmin ? (
                <>
                  <Button variant="outline" className="h-8" onClick={() => actions.onKeys(s)}>
                    Ingest keys
                  </Button>
                  <Button variant="ghost" className="h-8" onClick={() => actions.onArchive(s)}>
                    Archive
                  </Button>
                </>
              ) : null}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
