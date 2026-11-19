import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProjectsList } from "@/components/projects/projects-list";
import type { Project } from "@/lib/api/types";

afterEach(cleanup);

const project = (overrides: Partial<Project> = {}): Project => ({
  id: "6f1c2d3e-0000-4000-8000-000000000001",
  org_id: "6f1c2d3e-0000-4000-8000-0000000000aa",
  name: "Checkout API",
  slug: "checkout-api",
  description: "Payments and cart",
  created_by: null,
  created_at: "2026-11-17T09:30:00Z",
  updated_at: "2026-11-17T09:30:00Z",
  archived_at: null,
  ...overrides,
});

const noop = () => {};

describe("ProjectsList", () => {
  it("shows a busy frame with the column headers while loading", () => {
    render(<ProjectsList state={{ kind: "loading" }} canCreate onCreate={noop} onRetry={noop} />);
    const region = screen.getByRole("region", { name: "Projects" });
    expect(region.getAttribute("aria-busy")).toBe("true");
    expect(screen.getByText("Loading projects…")).toBeTruthy();
    expect(screen.getByText("Telemetry")).toBeTruthy();
  });

  it("shows the error inside the block with the request id, and retries", async () => {
    const onRetry = vi.fn();
    render(
      <ProjectsList
        state={{ kind: "error", message: "Internal server error.", requestId: "abcdef1234567890" }}
        canCreate
        onCreate={noop}
        onRetry={onRetry}
      />,
    );
    expect(screen.getByRole("alert").textContent).toMatch(
      /Couldn.t load projects\. Internal server error\./,
    );
    expect(screen.getByText("Request abcdef12")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("keeps the frame when empty and offers create only to those who can", async () => {
    const onCreate = vi.fn();
    const { rerender } = render(
      <ProjectsList state={{ kind: "ready", projects: [] }} canCreate onCreate={onCreate} onRetry={noop} />,
    );
    expect(screen.getByText(/No projects yet/)).toBeTruthy();
    expect(screen.getByText("Project")).toBeTruthy(); // header stays
    await userEvent.click(screen.getByRole("button", { name: "New project" }));
    expect(onCreate).toHaveBeenCalledOnce();

    rerender(
      <ProjectsList
        state={{ kind: "ready", projects: [] }}
        canCreate={false}
        onCreate={onCreate}
        onRetry={noop}
      />,
    );
    expect(screen.queryByRole("button", { name: "New project" })).toBeNull();
  });

  it("renders each project with its slug, telemetry status and ISO date", () => {
    render(
      <ProjectsList
        state={{
          kind: "ready",
          projects: [project(), project({ id: "x2", name: "Search", slug: "search", description: null })],
        }}
        canCreate
        onCreate={noop}
        onRetry={noop}
      />,
    );
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0].textContent).toContain("Checkout API");
    expect(items[0].textContent).toContain("checkout-api");
    expect(items[0].textContent).toContain("2026-11-17");
    expect(items[1].textContent).toContain("Search");
    expect(screen.getAllByText("No telemetry yet").length).toBeGreaterThanOrEqual(2);
  });

  it("offers load more only when there is a next page", async () => {
    const onLoadMore = vi.fn();
    const { rerender } = render(
      <ProjectsList state={{ kind: "ready", projects: [project()] }} canCreate onCreate={noop} onRetry={noop} />,
    );
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    rerender(
      <ProjectsList
        state={{ kind: "ready", projects: [project()] }}
        canCreate
        onCreate={noop}
        onRetry={noop}
        hasMore
        onLoadMore={onLoadMore}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(onLoadMore).toHaveBeenCalledOnce();
  });
});
