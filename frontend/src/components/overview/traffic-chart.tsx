"use client";

/*
 * Requests and errors per minute for one service over the last 60 minutes (one axis, same unit).
 * Minutes without rollups are gaps (null), never drawn as zero. Colors validated with the dataviz
 * validator (light surface): requests #2a78d6, errors #d03b3b.
 */
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import type { OverviewService } from "@/lib/api/types";

export const REQUESTS_COLOR = "#2a78d6";
export const ERRORS_COLOR = "#d03b3b";

export interface MinuteRow {
  minute: string; // "HH:MM" UTC
  requests: number | null;
  errors: number | null;
}

/** 60 one-minute slots ending at `generatedAt`'s minute; missing minutes are null. */
export function fillMinutes(series: OverviewService["series"], generatedAt: string, minutes = 60): MinuteRow[] {
  const byMinute = new Map(series.map((p) => [Date.parse(p.minute), p]));
  const end = Math.floor(Date.parse(generatedAt) / 60_000) * 60_000;
  const rows: MinuteRow[] = [];
  for (let t = end - (minutes - 1) * 60_000; t <= end; t += 60_000) {
    const p = byMinute.get(t);
    rows.push({ minute: new Date(t).toISOString().slice(11, 16), requests: p?.requests ?? null, errors: p?.errors ?? null });
  }
  return rows;
}

export function TrafficChart({ rows }: { rows: MinuteRow[] }) {
  return (
    <div className="h-36 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
          <CartesianGrid vertical={false} stroke="var(--rule)" strokeWidth={1} />
          <XAxis
            dataKey="minute"
            tickLine={false}
            axisLine={{ stroke: "var(--border-soft)" }}
            tick={{ fontSize: 11, fill: "var(--text-muted)" }}
            interval={14}
          />
          <YAxis
            allowDecimals={false}
            tickLine={false}
            axisLine={false}
            tick={{ fontSize: 11, fill: "var(--text-muted)" }}
            width={48}
          />
          <Tooltip
            cursor={{ stroke: "var(--control)", strokeWidth: 1 }}
            contentStyle={{ fontSize: 12, borderRadius: 6, borderColor: "var(--border-soft)" }}
            labelFormatter={(m) => `${String(m)}Z`}
            formatter={(value, name) => [value ?? "no data", name]}
          />
          <Line
            name="Requests/min"
            dataKey="requests"
            stroke={REQUESTS_COLOR}
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 4, stroke: "var(--surface)", strokeWidth: 2 }}
            connectNulls={false}
            isAnimationActive={false}
          />
          <Line
            name="Errors/min"
            dataKey="errors"
            stroke={ERRORS_COLOR}
            strokeWidth={2}
            dot={false}
            activeDot={{ r: 4, stroke: "var(--surface)", strokeWidth: 2 }}
            connectNulls={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

export function ChartLegend() {
  return (
    <div className="flex gap-4 text-xs text-muted-foreground" aria-hidden>
      <span className="flex items-center gap-1.5">
        <span className="h-0.5 w-3 rounded-full" style={{ background: REQUESTS_COLOR }} />
        Requests/min
      </span>
      <span className="flex items-center gap-1.5">
        <span className="h-0.5 w-3 rounded-full" style={{ background: ERRORS_COLOR }} />
        Errors/min
      </span>
    </div>
  );
}
