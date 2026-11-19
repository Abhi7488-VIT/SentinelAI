"use client";

/*
 * Service map: React Flow over the deterministic layered layout. Edge style by source (declared =
 * dashed, observed = solid, both = solid + thicker), node border by health, externals as separate
 * grey nodes. Clicking a service opens a panel with its edges. The edge table below the canvas is
 * the accessible (and testable) view of the same data.
 */
import "@xyflow/react/dist/style.css";

import { useQuery } from "@tanstack/react-query";
import { Background, Controls, type Edge, MarkerType, type Node, ReactFlow } from "@xyflow/react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { HealthPill, Pill } from "@/components/common/pills";
import { EmptyBlock, ErrorBlock, errorInfo, SkeletonBars } from "@/components/common/states";
import { useProject } from "@/components/project/project-frame";
import { layout, type MergedEdge, mergeEdges } from "@/components/service-map/layout";
import { useOrg } from "@/components/shell/org-context";
import { api, unwrap } from "@/lib/api/client";
import { keys, POLL_MS } from "@/lib/api/keys";
import type { GraphNode, HealthStatus, ServiceGraph } from "@/lib/api/types";
import { formatAgo } from "@/lib/format";

const HEALTH_BORDER: Record<HealthStatus, string> = {
  healthy: "var(--positive)",
  degraded: "var(--warning)",
  unhealthy: "var(--negative)",
  unknown: "var(--control)",
};

const SOURCE_LABEL = { declared: "Declared", observed: "Observed", both: "Declared + observed" } as const;

function toFlow(graph: ServiceGraph, selected: string | null): { nodes: Node[]; edges: Edge[] } {
  const merged = mergeEdges(graph.edges);
  const nodes: Node[] = layout(graph.nodes, merged).map(({ node, x, y }) => {
    const external = node.kind === "external";
    return {
      id: node.id,
      position: { x, y },
      data: { label: node.name },
      connectable: false,
      style: {
        width: 200,
        fontSize: 12,
        fontFamily: "var(--font-mono)",
        borderRadius: external ? 999 : 8,
        border: `${node.id === selected ? 3 : 2}px solid ${external ? "var(--control)" : HEALTH_BORDER[node.health_status ?? "unknown"]}`,
        background: external ? "var(--raised)" : "var(--surface)",
        color: "var(--text)",
      },
      sourcePosition: "right" as Node["sourcePosition"],
      targetPosition: "left" as Node["targetPosition"],
    };
  });
  const edges: Edge[] = merged.map((e) => ({
    id: e.id,
    source: e.from,
    target: e.to,
    markerEnd: { type: MarkerType.ArrowClosed, color: "var(--text-muted)" },
    style: {
      stroke: "var(--text-muted)",
      strokeWidth: e.source === "both" ? 2.5 : 1.5,
      strokeDasharray: e.source === "declared" ? "6 4" : undefined,
    },
  }));
  return { nodes, edges };
}

export function EdgeTable({ graph }: { graph: ServiceGraph }) {
  const names = new Map(graph.nodes.map((n) => [n.id, n.name]));
  const merged = mergeEdges(graph.edges);
  if (merged.length === 0) return null;
  return (
    <table className="num w-full text-left text-sm">
      <caption className="sr-only">Dependencies</caption>
      <thead className="text-xs text-muted-foreground">
        <tr>
          <th className="py-1 font-normal">From</th>
          <th className="py-1 font-normal">To</th>
          <th className="py-1 font-normal">Source</th>
          <th className="py-1 font-normal">Calls (latest 2 min)</th>
        </tr>
      </thead>
      <tbody>
        {merged.map((e: MergedEdge) => (
          <tr key={e.id} className="border-t border-rule">
            <td className="py-1 font-mono text-[13px]">{names.get(e.from)}</td>
            <td className="py-1 font-mono text-[13px]">{names.get(e.to)}</td>
            <td className="py-1">{SOURCE_LABEL[e.source]}</td>
            <td className="py-1">{e.source === "declared" ? "–" : e.callCount}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ServicePanel({ node, graph, servicesHref }: { node: GraphNode; graph: ServiceGraph; servicesHref: string }) {
  const names = new Map(graph.nodes.map((n) => [n.id, n.name]));
  const merged = mergeEdges(graph.edges);
  const calls = merged.filter((e) => e.from === node.id);
  const callers = merged.filter((e) => e.to === node.id);
  return (
    <aside aria-label={`Service ${node.name}`} className="grid content-start gap-3 rounded-md border border-border p-4">
      <div className="flex items-center gap-2">
        <span className="font-mono text-sm font-medium">{node.name}</span>
        {node.kind === "service" ? <HealthPill status={node.health_status} /> : <Pill tone="neutral">External</Pill>}
      </div>
      {node.kind === "service" ? (
        <p className="text-xs text-muted-foreground">
          {node.environment} · telemetry {formatAgo(node.last_seen_at ?? null)}
        </p>
      ) : null}
      <div className="grid gap-1 text-sm">
        <p className="text-xs text-muted-foreground">Calls</p>
        {calls.length ? calls.map((e) => <p key={e.id} className="font-mono text-[13px]">→ {names.get(e.to)} <span className="text-muted-foreground">({SOURCE_LABEL[e.source].toLowerCase()})</span></p>) : <p className="text-muted-foreground">Nothing recorded.</p>}
        <p className="mt-2 text-xs text-muted-foreground">Called by</p>
        {callers.length ? callers.map((e) => <p key={e.id} className="font-mono text-[13px]">← {names.get(e.from)} <span className="text-muted-foreground">({SOURCE_LABEL[e.source].toLowerCase()})</span></p>) : <p className="text-muted-foreground">Nothing recorded.</p>}
      </div>
      {node.kind === "service" ? (
        <Link href={servicesHref} className="text-sm underline-offset-4 hover:underline">
          Edit declared dependencies
        </Link>
      ) : null}
    </aside>
  );
}

export function ServiceMapView() {
  const { orgId } = useOrg();
  const project = useProject();
  const [selected, setSelected] = useState<string | null>(null);
  const query = useQuery({
    queryKey: keys.graph(orgId, project.id),
    queryFn: () =>
      unwrap(api.GET("/api/v1/orgs/{org_id}/service-graph", { params: { path: { org_id: orgId }, query: { project_id: project.id } } })),
    refetchInterval: POLL_MS,
  });
  const flow = useMemo(() => (query.data ? toFlow(query.data, selected) : null), [query.data, selected]);
  const servicesHref = `/orgs/${orgId}/projects/${project.id}/services`;

  if (query.isPending)
    return (
      <div className="px-4 md:px-6">
        <SkeletonBars rows={4} label="service map" />
      </div>
    );
  if (query.isError)
    return (
      <div className="px-4 md:px-6">
        <ErrorBlock what="the service map" error={errorInfo(query.error)} onRetry={() => void query.refetch()} />
      </div>
    );
  const graph = query.data;
  if (graph.nodes.length === 0)
    return (
      <div className="px-4 md:px-6">
        <EmptyBlock action={<Link href={servicesHref} className="text-sm hover:underline">Add a service</Link>}>
          No services in this project yet.
        </EmptyBlock>
      </div>
    );
  const node = graph.nodes.find((n) => n.id === selected) ?? null;

  return (
    <div className="grid gap-4 px-4 md:px-6">
      <div className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted-foreground">
        <span>— Observed (from traces)</span>
        <span>- - Declared</span>
        <span>━ Both</span>
        <span>Border: health · grey pill: external dependency</span>
        {graph.truncated ? <span className="text-negative">Truncated at 5,000 edges</span> : null}
      </div>
      {graph.edges.length === 0 ? (
        <EmptyBlock>No dependencies yet: none declared, and none observed in traces in the last 7 days.</EmptyBlock>
      ) : null}
      <div className="grid gap-4 lg:grid-cols-[1fr_280px]">
        <div className="h-[480px] rounded-md border border-border" aria-label="Service map canvas">
          {flow ? (
            <ReactFlow
              nodes={flow.nodes}
              edges={flow.edges}
              fitView
              nodesDraggable={false}
              nodesConnectable={false}
              onNodeClick={(_, n) => setSelected(n.id)}
              onPaneClick={() => setSelected(null)}
            >
              <Background gap={24} color="var(--rule)" />
              <Controls showInteractive={false} />
            </ReactFlow>
          ) : null}
        </div>
        {node ? (
          <ServicePanel node={node} graph={graph} servicesHref={servicesHref} />
        ) : (
          <p className="text-sm text-muted-foreground">Select a node to see its calls.</p>
        )}
      </div>
      <EdgeTable graph={graph} />
    </div>
  );
}
