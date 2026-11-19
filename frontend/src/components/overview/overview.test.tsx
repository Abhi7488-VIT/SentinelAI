import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { OverviewBody } from "@/components/overview/overview-body";
import { fillMinutes } from "@/components/overview/traffic-chart";
import type { Overview, OverviewService } from "@/lib/api/types";

afterEach(cleanup);

const GEN = "2026-11-19T21:30:20Z";
const NOW = Date.parse(GEN);

const svc = (o: Partial<OverviewService> = {}): OverviewService => ({
  id: "s1",
  name: "order-service",
  environment: "production",
  health_status: "degraded",
  last_seen_at: "2026-11-19T21:30:10Z",
  ingest_accepted_15m: 12_345,
  ingest_rejected_15m: 3,
  series: [
    { minute: "2026-11-19T21:28:00Z", requests: 600, errors: 0 },
    { minute: "2026-11-19T21:30:00Z", requests: 590, errors: 120 },
  ],
  ...o,
});

const overview = (o: Partial<Overview> = {}): Overview => ({
  project_id: "p",
  generated_at: GEN,
  open_incidents: { critical: 1, warning: 2 },
  latest_incidents: [
    { id: "i1", number: 3, title: "Errors in order-service", status: "open", severity: "critical", started_at: "2026-11-19T21:16:45Z" },
  ],
  detections_24h: [
    { rule: "error_rate", count: 4 },
    { rule: "latency_shift", count: 3 },
  ],
  services: [svc()],
  services_truncated: false,
  ...o,
});

describe("fillMinutes", () => {
  it("keeps 60 slots and leaves missing minutes as gaps, not zeros", () => {
    const rows = fillMinutes(svc().series, GEN);
    expect(rows).toHaveLength(60);
    expect(rows.at(-1)).toEqual({ minute: "21:30", requests: 590, errors: 120 });
    expect(rows.at(-2)).toEqual({ minute: "21:29", requests: null, errors: null });
    expect(rows.at(-3)?.requests).toBe(600);
    expect(rows[0].minute).toBe("20:31");
  });
});

describe("OverviewBody", () => {
  it("reports the API's numbers", () => {
    render(<OverviewBody data={overview()} orgId="o" servicesHref="/s" chart={false} now={NOW} />);
    expect(screen.getByText("Open incidents").parentElement?.textContent).toContain("Open incidents3"); // 1 + 2
    expect(screen.getByText("1 critical · 2 warning")).toBeTruthy();
    expect(screen.getByText("Detections, last 24 h").parentElement?.textContent).toContain("24 h7");
    expect(screen.getByText("Error rate: 4")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Errors in order-service" }).getAttribute("href")).toBe("/orgs/o/incidents/i1");
    const panel = screen.getByText("order-service").closest("li");
    expect(panel?.textContent).toContain("Degraded");
    expect(panel?.textContent).toContain("telemetry 10 s ago");
    expect(panel?.textContent).toContain("Requests, 60 min1,190");
    expect(panel?.textContent).toContain("Errors120");
    expect(panel?.textContent).toContain("12,345");
    expect(screen.getByText("Show as table")).toBeTruthy();
  });

  it("says what is missing instead of drawing zeros", () => {
    render(
      <OverviewBody
        data={overview({
          open_incidents: { critical: 0, warning: 0 },
          latest_incidents: [],
          detections_24h: [],
          services: [svc({ series: [], last_seen_at: null, ingest_accepted_15m: 0, ingest_rejected_15m: 0, health_status: "unknown" })],
        })}
        orgId="o"
        servicesHref="/s"
        chart={false}
      />,
    );
    expect(screen.getByText("No rule has fired.")).toBeTruthy();
    expect(screen.getByText("No incidents in this project.")).toBeTruthy();
    expect(screen.getByText("No server requests recorded in the last 60 minutes.")).toBeTruthy();
    expect(screen.getByText("no telemetry yet")).toBeTruthy();
    expect(screen.queryByText("Show as table")).toBeNull();
  });

  it("points to service setup when the project has no services", () => {
    render(<OverviewBody data={overview({ services: [] })} orgId="o" servicesHref="/svc" chart={false} />);
    expect(screen.getByText(/No telemetry yet/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Add a service" }).getAttribute("href")).toBe("/svc");
  });
});
