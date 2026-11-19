/*
 * Overview of one project, from GET /overview. Presentational: tiles (open incidents, detections
 * in 24 h), latest incidents, and one panel per service (health, last telemetry, ingest
 * accepted/rejected, requests and errors per minute with a table view). Empty pieces say what is
 * missing instead of showing zeros that look like measurements.
 */
import Link from "next/link";

import { HealthPill, SeverityPill, StatusPill } from "@/components/common/pills";
import { EmptyBlock } from "@/components/common/states";
import { ChartLegend, fillMinutes, TrafficChart } from "@/components/overview/traffic-chart";
import type { Overview, OverviewService } from "@/lib/api/types";
import { formatAgo, formatCount, formatDateTime } from "@/lib/format";

const RULE_LABEL: Record<string, string> = {
  error_rate: "Error rate",
  latency_shift: "Latency shift",
  resource_saturation: "Resource saturation",
  health_check: "Health check",
  deploy_change: "Deploy change",
};

function Tile({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid content-start gap-1 rounded-md border border-border p-4">
      <p className="text-xs text-muted-foreground">{label}</p>
      {children}
    </div>
  );
}

function ServicePanel({ s, generatedAt, chart, now }: { s: OverviewService; generatedAt: string; chart: boolean; now?: number }) {
  const rows = fillMinutes(s.series, generatedAt);
  const withData = rows.filter((r) => r.requests !== null);
  const totals = withData.reduce((a, r) => ({ req: a.req + (r.requests ?? 0), err: a.err + (r.errors ?? 0) }), { req: 0, err: 0 });
  return (
    <li className="grid gap-3 rounded-md border border-border p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate font-mono text-sm font-medium">{s.name}</span>
          <HealthPill status={s.health_status} />
        </div>
        <span className="num text-xs text-muted-foreground" title={s.last_seen_at ?? undefined}>
          {s.last_seen_at ? `telemetry ${formatAgo(s.last_seen_at, now)}` : "no telemetry yet"}
        </span>
      </div>
      <dl className="num flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted-foreground">
        <div className="flex gap-1">
          <dt>Requests, 60 min</dt>
          <dd className="text-foreground">{formatCount(totals.req)}</dd>
        </div>
        <div className="flex gap-1">
          <dt>Errors</dt>
          <dd className="text-foreground">{formatCount(totals.err)}</dd>
        </div>
        <div className="flex gap-1">
          <dt>Ingest accepted, 15 min</dt>
          <dd className="text-foreground">{formatCount(s.ingest_accepted_15m)}</dd>
        </div>
        <div className="flex gap-1">
          <dt>rejected</dt>
          <dd className={s.ingest_rejected_15m > 0 ? "text-negative" : "text-foreground"}>{formatCount(s.ingest_rejected_15m)}</dd>
        </div>
      </dl>
      {withData.length === 0 ? (
        <p className="text-sm text-muted-foreground">No server requests recorded in the last 60 minutes.</p>
      ) : (
        <>
          <ChartLegend />
          {chart ? <TrafficChart rows={rows} /> : null}
          <details className="text-xs">
            <summary className="cursor-pointer text-muted-foreground">Show as table</summary>
            <table className="num mt-2 w-full text-left">
              <thead className="text-muted-foreground">
                <tr>
                  <th className="font-normal">Minute (UTC)</th>
                  <th className="font-normal">Requests</th>
                  <th className="font-normal">Errors</th>
                </tr>
              </thead>
              <tbody>
                {withData.map((r) => (
                  <tr key={r.minute}>
                    <td>{r.minute}</td>
                    <td>{r.requests}</td>
                    <td>{r.errors}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        </>
      )}
    </li>
  );
}

export function OverviewBody({
  data,
  orgId,
  servicesHref,
  chart = true,
  now,
}: {
  data: Overview;
  orgId: string;
  servicesHref: string;
  chart?: boolean;
  now?: number;
}) {
  const open = data.open_incidents.critical + data.open_incidents.warning;
  const detections = data.detections_24h.reduce((n, r) => n + r.count, 0);
  return (
    <div className="grid gap-6 px-4 md:px-6">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <Tile label="Open incidents">
          <p className="num text-2xl font-semibold">{open}</p>
          <p className="num text-xs text-muted-foreground">
            {data.open_incidents.critical} critical · {data.open_incidents.warning} warning
          </p>
        </Tile>
        <Tile label="Detections, last 24 h">
          <p className="num text-2xl font-semibold">{detections}</p>
          {data.detections_24h.length ? (
            <ul className="num text-xs text-muted-foreground">
              {data.detections_24h.map((r) => (
                <li key={r.rule}>
                  {RULE_LABEL[r.rule] ?? r.rule}: {r.count}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-muted-foreground">No rule has fired.</p>
          )}
        </Tile>
        <Tile label="Services">
          <p className="num text-2xl font-semibold">{data.services.length}</p>
          <p className="num text-xs text-muted-foreground">
            {data.services.filter((s) => s.last_seen_at).length} have sent telemetry
            {data.services_truncated ? " · showing the first 50" : ""}
          </p>
        </Tile>
      </div>

      <section aria-labelledby="latest" className="grid gap-2">
        <div className="flex items-baseline justify-between">
          <h2 id="latest" className="text-sm font-semibold">
            Latest incidents
          </h2>
          <Link href={`/orgs/${orgId}/incidents?project=${data.project_id}`} className="text-xs text-muted-foreground hover:underline">
            All incidents
          </Link>
        </div>
        {data.latest_incidents.length === 0 ? (
          <EmptyBlock>No incidents in this project.</EmptyBlock>
        ) : (
          <ul className="grid rounded-md border border-border">
            {data.latest_incidents.map((i) => (
              <li key={i.id} className="flex flex-wrap items-center gap-2 border-t border-rule px-3 py-2 first:border-t-0">
                <span className="num font-mono text-xs text-muted-foreground">#{i.number}</span>
                <Link href={`/orgs/${orgId}/incidents/${i.id}`} className="min-w-0 flex-1 truncate text-sm hover:underline">
                  {i.title}
                </Link>
                <SeverityPill severity={i.severity} />
                <StatusPill status={i.status} />
                <span className="num text-xs text-muted-foreground">{formatDateTime(i.started_at)}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="services" className="grid gap-2">
        <h2 id="services" className="text-sm font-semibold">
          Services
        </h2>
        {data.services.length === 0 ? (
          <EmptyBlock
            action={
              <Link href={servicesHref} className="text-sm underline-offset-4 hover:underline">
                Add a service
              </Link>
            }
          >
            No telemetry yet. Register a service and create an ingest key for its exporter.
          </EmptyBlock>
        ) : (
          <ul className="grid gap-3 lg:grid-cols-2">
            {data.services.map((s) => (
              <ServicePanel key={s.id} s={s} generatedAt={data.generated_at} chart={chart} now={now} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
