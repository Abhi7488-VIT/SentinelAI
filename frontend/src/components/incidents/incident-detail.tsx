/*
 * Incident detail sections (presentational): header with status actions, affected services, each
 * detection with its templated explanation, numbers and "grouped because" reasons, and the
 * timeline of stored events. Nothing here is inferred: every line comes from the API.
 */
import { Pill, SeverityPill, StatusPill } from "@/components/common/pills";
import { Button } from "@/components/ui/button";
import type { GroupingReason, IncidentDetail, IncidentDetection, IncidentEvent, IncidentStatus } from "@/lib/api/types";
import { formatDateTime, formatDuration, formatTime } from "@/lib/format";

// Mirrors the backend transition table (app/services/incidents.py); the server enforces it.
export const NEXT_STATUSES: Record<IncidentStatus, IncidentStatus[]> = {
  open: ["investigating", "resolved"],
  investigating: ["mitigated", "resolved"],
  mitigated: ["resolved"],
  resolved: ["open"],
};

const ACTION_LABEL: Record<IncidentStatus, string> = {
  open: "Reopen",
  investigating: "Start investigating",
  mitigated: "Mark mitigated",
  resolved: "Resolve",
};

const RULE_LABEL: Record<string, string> = {
  error_rate: "Error rate",
  latency_shift: "Latency shift",
  resource_saturation: "Resource saturation",
  health_check: "Health check",
  deploy_change: "Deploy change",
};

const REASON_LABEL: Record<GroupingReason["type"], string> = {
  shared_trace: "Shared traces",
  exception_signature: "Same exception",
  dependency_edge: "Dependency edge",
  deploy_proximity: "Deploy nearby",
  same_service: "Same service",
};

function num(v: number | null | undefined): string {
  if (v === null || v === undefined) return "–";
  return Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 1 ? v.toFixed(2) : v.toPrecision(2);
}

export function Header({
  incident,
  canTransition,
  pending,
  onTransition,
  now,
}: {
  incident: IncidentDetail;
  canTransition: boolean;
  pending: boolean;
  onTransition: (to: IncidentStatus) => void;
  now?: number;
}) {
  return (
    <div className="grid gap-3 px-4 md:px-6">
      <div className="flex flex-wrap items-center gap-2">
        <span className="num font-mono text-sm text-muted-foreground">#{incident.number}</span>
        <SeverityPill severity={incident.severity} />
        <StatusPill status={incident.status} />
      </div>
      <h1 className="text-[22px] leading-7 font-semibold">{incident.title}</h1>
      <dl className="num grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:flex sm:flex-wrap">
        <div>
          <dt className="text-xs text-muted-foreground">Started</dt>
          <dd>{formatDateTime(incident.started_at)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Detected</dt>
          <dd>{formatDateTime(incident.detected_at)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">{incident.resolved_at ? "Duration" : "Open for"}</dt>
          <dd>{formatDuration(incident.started_at, incident.resolved_at, now)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-foreground">Detections</dt>
          <dd>
            {incident.detections.length}
            {incident.detections_resolved_at ? " · all resolved" : ""}
          </dd>
        </div>
      </dl>
      {canTransition ? (
        <div className="flex flex-wrap gap-2" aria-label="Status actions">
          {NEXT_STATUSES[incident.status].map((to) => (
            <Button key={to} variant={to === "resolved" ? "default" : "outline"} className="h-8" disabled={pending} onClick={() => onTransition(to)}>
              {ACTION_LABEL[to]}
            </Button>
          ))}
        </div>
      ) : null}
      <p className="text-xs text-muted-foreground">
        SentinelAI never resolves an incident by itself; root-cause investigation arrives in a later phase.
      </p>
    </div>
  );
}

export function DetectionCard({ item, opener }: { item: IncidentDetection; opener: boolean }) {
  const d = item.detection;
  return (
    <li className="grid gap-2 border-t border-rule px-4 py-3 md:px-6">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">{RULE_LABEL[d.rule] ?? d.rule}</span>
        <SeverityPill severity={d.severity} />
        {d.resolved_at ? <Pill tone="positive">Resolved {formatTime(d.resolved_at)}</Pill> : <Pill tone="negative">Firing</Pill>}
        <span className="num text-xs text-muted-foreground">
          started {formatTime(d.started_at)} · fired {formatTime(d.fired_at)}
        </span>
      </div>
      <p className="text-sm">{d.explanation}</p>
      <dl className="num flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted-foreground">
        <div className="flex gap-1">
          <dt>{d.metric}</dt>
        </div>
        <div className="flex gap-1">
          <dt>observed</dt>
          <dd className="text-foreground">{num(d.observed_value)}</dd>
        </div>
        <div className="flex gap-1">
          <dt>baseline</dt>
          <dd className="text-foreground">{num(d.baseline_value)}</dd>
        </div>
        <div className="flex gap-1">
          <dt>threshold</dt>
          <dd className="text-foreground">{num(d.threshold)}</dd>
        </div>
        <div className="flex gap-1">
          <dt>peak</dt>
          <dd className="text-foreground">{num(d.peak_value)}</dd>
        </div>
      </dl>
      <div className="grid gap-1">
        <p className="text-xs font-medium text-muted-foreground">
          {opener ? "Opened this incident" : item.reasons.length ? `Grouped because (score ${item.score})` : "Grouped (no reason recorded)"}
        </p>
        {item.reasons.length ? (
          <ul className="grid gap-1">
            {item.reasons.map((r) => (
              <li key={r.type} className="flex flex-wrap items-baseline gap-2 text-sm">
                <Pill tone="neutral">{REASON_LABEL[r.type]}</Pill>
                <span className="text-muted-foreground">{r.detail}</span>
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    </li>
  );
}

export function describeEvent(e: IncidentEvent): string {
  const p = e.payload as Record<string, unknown>;
  switch (e.type) {
    case "created":
      return `Opened by ${RULE_LABEL[String(p.rule)] ?? String(p.rule)} on ${String(p.service)}`;
    case "detection_added":
      return `${RULE_LABEL[String(p.rule)] ?? String(p.rule)} on ${String(p.service)} linked${
        Number(p.score) > 0 ? ` (score ${String(p.score)})` : ""
      }`;
    case "severity_changed":
      return `Severity ${String(p.from)} → ${String(p.to)}`;
    case "all_detections_resolved":
      return `All ${String(p.detections)} detections resolved; incident stays open until a person resolves it`;
    case "status_changed":
      return `Status ${String(p.from)} → ${String(p.to)}${p.note ? `: ${String(p.note)}` : ""}`;
    default:
      return e.type;
  }
}

export function Timeline({ events }: { events: IncidentEvent[] }) {
  if (events.length === 0) return <p className="px-4 text-sm text-muted-foreground md:px-6">No events yet.</p>;
  return (
    <ol className="grid">
      {events.map((e) => (
        <li key={e.id} className="flex flex-wrap gap-x-3 border-t border-rule px-4 py-2 text-sm md:px-6">
          <span className="num w-36 flex-none text-muted-foreground">{formatDateTime(e.created_at)}</span>
          <span className="min-w-0 flex-1">{describeEvent(e)}</span>
          <span className="text-xs text-muted-foreground">{e.actor === "user" ? "person" : "system"}</span>
        </li>
      ))}
    </ol>
  );
}
