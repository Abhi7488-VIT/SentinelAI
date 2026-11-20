import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { type ServiceActions, ServicesList } from "@/components/services/services-list";
import type { Service } from "@/lib/api/types";

afterEach(cleanup);

const NOW = Date.parse("2026-11-19T12:00:00Z");

const service = (o: Partial<Service> = {}): Service => ({
  id: "s1",
  org_id: "o",
  project_id: "p",
  name: "order-service",
  description: null,
  environment: "production",
  repo_url: null,
  deployment: {},
  telemetry_config: {},
  health_status: "healthy",
  last_seen_at: "2026-11-19T11:59:30Z",
  archived_at: null,
  created_by: null,
  created_at: "2026-11-19T00:00:00Z",
  updated_at: "2026-11-19T00:00:00Z",
  dependencies: [],
  ...o,
});

const actions = (o: Partial<ServiceActions> = {}): ServiceActions => ({
  canEdit: true,
  canAdmin: true,
  onDependencies: vi.fn(),
  onKeys: vi.fn(),
  onArchive: vi.fn(),
  ...o,
});

describe("ServicesList", () => {
  it("is busy while loading", () => {
    render(<ServicesList state={{ kind: "loading" }} actions={actions()} onRetry={vi.fn()} />);
    expect(screen.getByText("Loading services…")).toBeTruthy();
  });

  it("shows the error with request id and retries", async () => {
    const onRetry = vi.fn();
    render(
      <ServicesList
        state={{ kind: "error", error: { message: "Boom.", requestId: "1234567890ab" } }}
        actions={actions()}
        onRetry={onRetry}
      />,
    );
    expect(screen.getByRole("alert").textContent).toContain("Couldn't load services. Boom.");
    expect(screen.getByText("Request 12345678")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("explains what to do when empty", async () => {
    const onCreate = vi.fn();
    render(<ServicesList state={{ kind: "ready", services: [] }} actions={actions()} onRetry={vi.fn()} onCreate={onCreate} />);
    expect(screen.getByText(/No services yet/).textContent).toContain("ingest key");
    await userEvent.click(screen.getByRole("button", { name: "New service" }));
    expect(onCreate).toHaveBeenCalledOnce();
  });

  it("shows real health, last telemetry and declared dependencies by name", () => {
    const db = service({ id: "s2", name: "payment-service", health_status: "unknown", last_seen_at: null });
    render(
      <ServicesList
        state={{
          kind: "ready",
          services: [
            service({
              dependencies: [
                { id: "d1", target_service_id: "s2", target_external: null, description: null, created_at: "" },
                { id: "d2", target_service_id: null, target_external: "postgresql:orders", description: null, created_at: "" },
              ],
            }),
            db,
          ],
        }}
        actions={actions()}
        onRetry={vi.fn()}
        now={NOW}
      />,
    );
    const [order, payment] = screen.getAllByRole("listitem");
    expect(order.textContent).toContain("Healthy");
    expect(order.textContent).toContain("30 s ago");
    expect(order.textContent).toContain("payment-service, postgresql:orders");
    expect(payment.textContent).toContain("No data");
    expect(payment.textContent).toContain("No telemetry yet");
    expect(payment.textContent).toContain("None declared");
  });

  it("offers actions by role", async () => {
    const a = actions({ canAdmin: false });
    const { rerender } = render(
      <ServicesList state={{ kind: "ready", services: [service()] }} actions={a} onRetry={vi.fn()} />,
    );
    expect(screen.queryByRole("button", { name: "Ingest keys" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Dependencies" }));
    expect(a.onDependencies).toHaveBeenCalledOnce();
    rerender(
      <ServicesList
        state={{ kind: "ready", services: [service()] }}
        actions={actions({ canEdit: false, canAdmin: false })}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button")).toBeNull();
  });
});
