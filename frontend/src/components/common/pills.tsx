import type { HealthStatus, IncidentStatus, Severity } from "@/lib/api/types";

const BASE = "inline-flex h-5 flex-none items-center gap-1.5 rounded-sm px-1.5 text-xs whitespace-nowrap";

const TONE = {
  neutral: "bg-pill-neutral text-pill-neutral-ink",
  negative: "bg-pill-negative text-pill-negative-ink",
  warning: "bg-pill-warning text-pill-warning-ink",
  positive: "bg-pill-positive text-pill-positive-ink",
} as const;

type Tone = keyof typeof TONE;

export function Pill({ tone, children }: { tone: Tone; children: React.ReactNode }) {
  return <span className={`${BASE} ${TONE[tone]}`}>{children}</span>;
}

const HEALTH: Record<HealthStatus, { tone: Tone; label: string }> = {
  healthy: { tone: "positive", label: "Healthy" },
  degraded: { tone: "warning", label: "Degraded" },
  unhealthy: { tone: "negative", label: "Unhealthy" },
  unknown: { tone: "neutral", label: "No data" },
};

export function HealthPill({ status }: { status: HealthStatus | null | undefined }) {
  const h = HEALTH[status ?? "unknown"];
  return <Pill tone={h.tone}>{h.label}</Pill>;
}

export function SeverityPill({ severity }: { severity: Severity }) {
  return <Pill tone={severity === "critical" ? "negative" : "warning"}>{severity === "critical" ? "Critical" : "Warning"}</Pill>;
}

const STATUS: Record<IncidentStatus, { tone: Tone; label: string }> = {
  open: { tone: "negative", label: "Open" },
  investigating: { tone: "warning", label: "Investigating" },
  mitigated: { tone: "neutral", label: "Mitigated" },
  resolved: { tone: "positive", label: "Resolved" },
};

export function StatusPill({ status }: { status: IncidentStatus }) {
  const s = STATUS[status];
  return <Pill tone={s.tone}>{s.label}</Pill>;
}
