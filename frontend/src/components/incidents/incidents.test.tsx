import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DetectionCard, Header, Timeline, describeEvent } from "@/components/incidents/incident-detail";
import { IncidentsList } from "@/components/incidents/incidents-list";
import { readFilters, toQuery } from "@/components/incidents/incidents-view";
import type { Detection, Incident, IncidentDetail, IncidentEvent } from "@/lib/api/types";

afterEach(cleanup);

const NOW = Date.parse("2026-11-19T21:30:00Z");

const incident = (o: Partial<Incident> = {}): Incident => ({
  id: "i1",
  project_id: "p",
  number: 3,
  title: "Latency, errors and resource saturation in api-gateway, frontend, order-service",
  status: "open",
  severity: "critical",
  started_at: "2026-11-19T21:16:45Z",
  detected_at: "2026-11-19T21:17:45Z",
  last_activity_at: "2026-11-19T21:18:45Z",
  resolved_at: null,
  detections_resolved_at: null,
  created_at: "2026-11-19T21:18:30Z",
  updated_at: "2026-11-19T21:18:30Z",
  services: [
    { id: "a", name: "api-gateway", environment: "production" },
    { id: "f", name: "frontend", environment: "production" },
  ],
  ...o,
});

const detection = (o: Partial<Detection> = {}): Detection => ({
  id: "d1",
  service_id: "o",
  rule: "error_rate",
  severity: "warning",
  metric: "error_ratio_60s",
  observed_value: 0.42,
  baseline_value: 0,
  threshold: 0.05,
  peak_value: 0.87,
  last_value: 0.5,
  started_at: "2026-11-19T21:16:45Z",
  fired_at: "2026-11-19T21:17:45Z",
  resolved_at: null,
  explanation: "order-service: 42% of requests failed over 60 s (threshold 5%).",
  evidence: {},
  created_at: "2026-11-19T21:17:45Z",
  updated_at: "2026-11-19T21:17:45Z",
  ...o,
});

const event = (o: Partial<IncidentEvent>): IncidentEvent => ({
  id: 1,
  incident_id: "i1",
  type: "created",
  actor: "system",
  actor_user_id: null,
  payload: {},
  created_at: "2026-11-19T21:18:30Z",
  ...o,
});

describe("IncidentsList", () => {
  it("has loading, error, and two different empty states", async () => {
    const onRetry = vi.fn();
    const { rerender } = render(<IncidentsList state={{ kind: "loading" }} orgId="o" onRetry={onRetry} />);
    expect(screen.getByText("Loading incidents…")).toBeTruthy();
    rerender(<IncidentsList state={{ kind: "error", error: { message: "Down.", requestId: null } }} orgId="o" onRetry={onRetry} />);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledOnce();
    rerender(<IncidentsList state={{ kind: "ready", incidents: [], filtered: false }} orgId="o" onRetry={onRetry} />);
    expect(screen.getByText(/open automatically when detections fire/)).toBeTruthy();
    rerender(<IncidentsList state={{ kind: "ready", incidents: [], filtered: true }} orgId="o" onRetry={onRetry} />);
    expect(screen.getByText("No incidents match these filters.")).toBeTruthy();
  });

  it("links each incident and shows its services, severity, status and duration", () => {
    render(<IncidentsList state={{ kind: "ready", incidents: [incident()], filtered: false }} orgId="o" onRetry={vi.fn()} now={NOW} />);
    const row = screen.getByRole("listitem");
    expect(row.textContent).toContain("#3");
    expect(row.textContent).toContain("api-gateway, frontend");
    expect(row.textContent).toContain("Critical");
    expect(row.textContent).toContain("Open");
    expect(row.textContent).toContain("13 min 15 s so far");
    expect(screen.getByRole("link").getAttribute("href")).toBe("/orgs/o/incidents/i1");
  });
});

describe("filters", () => {
  it("drops invalid URL values and computes the time range", () => {
    const f = readFilters(new URLSearchParams("status=bogus&severity=critical&range=1h&q=%20%231%20&service=s"));
    const q = toQuery(f, NOW);
    expect(q.status).toBeUndefined();
    expect(q.severity).toBe("critical");
    expect(q.from).toBe("2026-11-19T20:30:00.000Z");
    expect(q.q).toBe("#1");
    expect(q.service_id).toBeUndefined(); // service only within a project
  });
});

describe("incident detail", () => {
  const detail = (o: Partial<IncidentDetail> = {}): IncidentDetail => ({ ...incident(), detections: [], ...o });

  it("offers only the transitions the server allows, to engineers", async () => {
    const onTransition = vi.fn();
    const { rerender } = render(<Header incident={detail()} canTransition pending={false} onTransition={onTransition} now={NOW} />);
    const actions = screen.getAllByRole("button").map((b) => b.textContent);
    expect(actions).toEqual(["Start investigating", "Resolve"]);
    await userEvent.click(screen.getByRole("button", { name: "Resolve" }));
    expect(onTransition).toHaveBeenCalledWith("resolved");
    rerender(<Header incident={detail({ status: "resolved", resolved_at: "2026-11-19T21:20:00Z" })} canTransition pending={false} onTransition={onTransition} now={NOW} />);
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["Reopen"]);
    rerender(<Header incident={detail()} canTransition={false} pending={false} onTransition={onTransition} now={NOW} />);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("shows a detection's explanation, numbers and grouping reasons", () => {
    render(
      <ul>
        <DetectionCard
          opener={false}
          item={{
            detection: detection(),
            score: 5,
            linked_at: "2026-11-19T21:18:00Z",
            reasons: [
              { type: "shared_trace", detail: "5 sample trace(s) have error spans on both sides", refs: [] },
              { type: "dependency_edge", detail: "direct dependency edge to 1 incident service(s)", refs: [] },
            ],
          }}
        />
      </ul>,
    );
    const card = screen.getAllByRole("listitem")[0];
    expect(card.textContent).toContain("42% of requests failed");
    expect(card.textContent).toContain("observed0.42");
    expect(card.textContent).toContain("peak0.87");
    expect(card.textContent).toContain("Grouped because (score 5)");
    expect(card.textContent).toContain("Shared traces");
    expect(card.textContent).toContain("Dependency edge");
  });

  it("marks the opener instead of claiming a missing reason", () => {
    render(
      <ul>
        <DetectionCard opener item={{ detection: detection(), score: 0, linked_at: "", reasons: [] }} />
      </ul>,
    );
    expect(screen.getByText("Opened this incident")).toBeTruthy();
  });

  it("describes stored events in words", () => {
    expect(describeEvent(event({ payload: { rule: "error_rate", service: "order-service" } }))).toBe(
      "Opened by Error rate on order-service",
    );
    expect(describeEvent(event({ type: "severity_changed", payload: { from: "warning", to: "critical" } }))).toBe(
      "Severity warning → critical",
    );
    render(<Timeline events={[event({ type: "status_changed", actor: "user", payload: { from: "open", to: "investigating" } })]} />);
    expect(screen.getByRole("listitem").textContent).toContain("Status open → investigating");
    expect(screen.getByRole("listitem").textContent).toContain("person");
  });
});
