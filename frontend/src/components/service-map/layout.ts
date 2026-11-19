/*
 * Deterministic layered layout for the service map (no layout library, plan §9).
 *
 * 1. Merge edges per (from, to): source = declared | observed | both.
 * 2. Layer = longest path from a node with no callers (callers left, callees right). Cycles are
 *    cut by ignoring edges that close a cycle in a DFS ordered by node name, so the result never
 *    depends on API order.
 * 3. Within a layer, nodes are sorted by (kind: services before externals, name) and stacked.
 */
import type { GraphEdge, GraphNode } from "@/lib/api/types";

export type EdgeSource = "declared" | "observed" | "both";

export interface MergedEdge {
  id: string;
  from: string;
  to: string;
  source: EdgeSource;
  callCount: number;
  sampleTraceId: string | null;
}

export interface Positioned {
  node: GraphNode;
  layer: number;
  x: number;
  y: number;
}

export const COL_WIDTH = 260;
export const ROW_HEIGHT = 96;

export function mergeEdges(edges: GraphEdge[]): MergedEdge[] {
  const byPair = new Map<string, MergedEdge>();
  for (const e of edges) {
    const key = `${e.from_node}->${e.to_node}`;
    const prev = byPair.get(key);
    if (!prev) {
      byPair.set(key, {
        id: key,
        from: e.from_node,
        to: e.to_node,
        source: e.source,
        callCount: e.source === "observed" ? e.call_count : 0,
        sampleTraceId: e.sample_trace_id ?? null,
      });
      continue;
    }
    if (prev.source !== e.source) prev.source = "both";
    if (e.source === "observed") prev.callCount += e.call_count;
    prev.sampleTraceId ??= e.sample_trace_id ?? null;
  }
  return [...byPair.values()].sort((a, b) => a.id.localeCompare(b.id));
}

function byName(nodes: Map<string, GraphNode>) {
  return (a: string, b: string) => {
    const na = nodes.get(a);
    const nb = nodes.get(b);
    const ka = `${na?.kind === "external" ? 1 : 0}:${na?.name ?? a}`;
    const kb = `${nb?.kind === "external" ? 1 : 0}:${nb?.name ?? b}`;
    return ka.localeCompare(kb);
  };
}

export function layout(nodes: GraphNode[], edges: MergedEdge[]): Positioned[] {
  const index = new Map(nodes.map((n) => [n.id, n]));
  const cmp = byName(index);
  const out = new Map<string, string[]>();
  for (const n of nodes) out.set(n.id, []);
  for (const e of edges) {
    if (index.has(e.from) && index.has(e.to) && e.from !== e.to) out.get(e.from)?.push(e.to);
  }
  for (const list of out.values()) list.sort(cmp);

  // DFS in name order; keep only edges that do not close a cycle (a DAG).
  const state = new Map<string, 0 | 1 | 2>();
  const dag = new Map<string, string[]>(nodes.map((n) => [n.id, []]));
  const visit = (id: string) => {
    state.set(id, 1);
    for (const to of out.get(id) ?? []) {
      const s = state.get(to) ?? 0;
      if (s === 1) continue; // back edge → cycle; drawn, but ignored for layering
      dag.get(id)?.push(to);
      if (s === 0) visit(to);
    }
    state.set(id, 2);
  };
  const order = [...index.keys()].sort(cmp);
  const hasCaller = new Set(edges.map((e) => e.to));
  for (const id of order.filter((i) => !hasCaller.has(i))) if (!state.get(id)) visit(id);
  for (const id of order) if (!state.get(id)) visit(id);

  // Longest path layering over the DAG (Kahn order).
  const indeg = new Map<string, number>(order.map((id) => [id, 0]));
  for (const tos of dag.values()) for (const to of tos) indeg.set(to, (indeg.get(to) ?? 0) + 1);
  const layer = new Map<string, number>(order.map((id) => [id, 0]));
  const queue = order.filter((id) => indeg.get(id) === 0);
  while (queue.length) {
    const id = queue.shift() as string;
    for (const to of dag.get(id) ?? []) {
      layer.set(to, Math.max(layer.get(to) ?? 0, (layer.get(id) ?? 0) + 1));
      indeg.set(to, (indeg.get(to) ?? 0) - 1);
      if (indeg.get(to) === 0) queue.push(to);
    }
  }

  const columns = new Map<number, string[]>();
  for (const id of order) {
    const l = layer.get(id) ?? 0;
    columns.set(l, [...(columns.get(l) ?? []), id]);
  }
  const placed: Positioned[] = [];
  for (const [l, ids] of [...columns.entries()].sort((a, b) => a[0] - b[0])) {
    ids.forEach((id, row) => {
      placed.push({ node: index.get(id) as GraphNode, layer: l, x: l * COL_WIDTH, y: row * ROW_HEIGHT });
    });
  }
  return placed;
}
