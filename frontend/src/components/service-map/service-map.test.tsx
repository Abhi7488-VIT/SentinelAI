import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { COL_WIDTH, layout, mergeEdges } from "@/components/service-map/layout";
import { EdgeTable } from "@/components/service-map/service-map-view";
import type { GraphEdge, GraphNode, ServiceGraph } from "@/lib/api/types";

afterEach(cleanup);

const svc = (name: string): GraphNode => ({ id: `service:${name}`, kind: "service", name, health_status: "healthy" });
const ext = (name: string): GraphNode => ({ id: `external:${name}`, kind: "external", name });
let n = 0;
const edge = (from: string, to: string, source: "declared" | "observed", calls = 0): GraphEdge => ({
  id: `e${n++}`,
  from_node: from,
  to_node: to,
  source,
  call_count: calls,
  first_seen_at: "",
  last_seen_at: "",
});

// The testbed shop's observed shape.
const NODES = [svc("payment-service"), svc("order-service"), ext("postgresql:orders"), svc("frontend"), svc("api-gateway"), svc("user-service")];
const EDGES = [
  edge("service:frontend", "service:api-gateway", "observed", 40),
  edge("service:frontend", "service:api-gateway", "declared"),
  edge("service:api-gateway", "service:order-service", "observed", 20),
  edge("service:api-gateway", "service:user-service", "declared"),
  edge("service:order-service", "service:payment-service", "observed", 5),
  edge("service:order-service", "external:postgresql:orders", "observed", 30),
];

describe("mergeEdges", () => {
  it("merges declared + observed per pair and sums observed calls", () => {
    const m = mergeEdges(EDGES);
    expect(m).toHaveLength(5);
    const fa = m.find((e) => e.from === "service:frontend");
    expect(fa?.source).toBe("both");
    expect(fa?.callCount).toBe(40);
    expect(m.find((e) => e.to === "service:user-service")?.source).toBe("declared");
  });
});

describe("layout", () => {
  it("puts callers left of callees, layer by longest path", () => {
    const pos = Object.fromEntries(layout(NODES, mergeEdges(EDGES)).map((p) => [p.node.name, p]));
    expect(pos["frontend"].layer).toBe(0);
    expect(pos["api-gateway"].layer).toBe(1);
    expect(pos["order-service"].layer).toBe(2);
    expect(pos["user-service"].layer).toBe(2);
    expect(pos["payment-service"].layer).toBe(3);
    expect(pos["postgresql:orders"].layer).toBe(3);
    expect(pos["payment-service"].x).toBe(3 * COL_WIDTH);
    // services before externals within a layer
    expect(pos["payment-service"].y).toBeLessThan(pos["postgresql:orders"].y);
  });

  it("does not depend on API order and survives cycles", () => {
    const a = layout(NODES, mergeEdges(EDGES));
    const b = layout([...NODES].reverse(), mergeEdges([...EDGES].reverse()));
    expect(b).toEqual(a);
    const cyc = layout([svc("a"), svc("b")], mergeEdges([edge("service:a", "service:b", "observed"), edge("service:b", "service:a", "observed")]));
    expect(cyc.map((p) => [p.node.name, p.layer])).toEqual([
      ["a", 0],
      ["b", 1],
    ]);
  });
});

describe("EdgeTable", () => {
  it("lists every dependency with its source and calls", () => {
    const graph: ServiceGraph = { project_id: "p", nodes: NODES, edges: EDGES, truncated: false };
    render(<EdgeTable graph={graph} />);
    const rows = screen.getAllByRole("row").slice(1).map((r) => r.textContent);
    expect(rows).toContain("frontendapi-gatewayDeclared + observed40");
    expect(rows).toContain("api-gatewayuser-serviceDeclared–");
    expect(rows).toContain("order-servicepostgresql:ordersObserved30");
  });
});
