"use client";

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { messageFor } from "@/components/auth/login-form";
import { ErrorBlock, errorInfo, SkeletonBars } from "@/components/common/states";
import { FormError } from "@/components/form-field";
import { DetectionCard, Header, Timeline } from "@/components/incidents/incident-detail";
import { useOrg } from "@/components/shell/org-context";
import { Button } from "@/components/ui/button";
import { api, unwrap } from "@/lib/api/client";
import { keys, POLL_MS } from "@/lib/api/keys";
import { type IncidentStatus, roleAtLeast } from "@/lib/api/types";

export function IncidentDetailView() {
  const { orgId, membership } = useOrg();
  const { incidentId } = useParams<{ incidentId: string }>();
  const queryClient = useQueryClient();
  const [actionError, setActionError] = useState<string | null>(null);
  const path = { org_id: orgId, incident_id: incidentId };

  const detail = useQuery({
    queryKey: keys.incident(orgId, incidentId),
    queryFn: () => unwrap(api.GET("/api/v1/orgs/{org_id}/incidents/{incident_id}", { params: { path } })),
    refetchInterval: POLL_MS,
  });
  const events = useInfiniteQuery({
    queryKey: keys.incidentEvents(orgId, incidentId),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/api/v1/orgs/{org_id}/incidents/{incident_id}/events", {
          params: { path, query: { after_id: pageParam ? Number(pageParam) : undefined, limit: 100 } },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: POLL_MS,
  });
  const transition = useMutation({
    mutationFn: (to: IncidentStatus) =>
      unwrap(api.POST("/api/v1/orgs/{org_id}/incidents/{incident_id}/transitions", { params: { path }, body: { to } })),
    onSuccess: async () => {
      setActionError(null);
      await queryClient.invalidateQueries({ queryKey: keys.incident(orgId, incidentId) });
      await queryClient.invalidateQueries({ queryKey: keys.incidentEvents(orgId, incidentId) });
      await queryClient.invalidateQueries({ queryKey: ["incidents", orgId] });
    },
    onError: (e) => setActionError(messageFor(e)),
  });

  const back = (
    <p className="px-4 text-xs text-muted-foreground md:px-6">
      <Link href={`/orgs/${orgId}/incidents`} className="hover:underline">
        Incidents
      </Link>{" "}
      /
    </p>
  );

  if (detail.isPending)
    return (
      <div className="grid gap-4 pt-6 pb-10">
        {back}
        <div className="px-4 md:px-6">
          <SkeletonBars rows={4} label="incident" />
        </div>
      </div>
    );
  if (detail.isError)
    return (
      <div className="grid gap-4 pt-6 pb-10">
        {back}
        <div className="px-4 md:px-6">
          <ErrorBlock what="this incident" error={errorInfo(detail.error)} onRetry={() => void detail.refetch()} />
        </div>
      </div>
    );

  const incident = detail.data;
  const allEvents = events.data?.pages.flatMap((p) => p.items) ?? [];
  const created = allEvents.find((e) => e.type === "created");
  const openerId = created ? String((created.payload as Record<string, unknown>).detection_id) : null;

  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-6 pt-6 pb-10">
      {back}
      <Header
        incident={incident}
        canTransition={roleAtLeast(membership.role, "engineer")}
        pending={transition.isPending}
        onTransition={(to) => transition.mutate(to)}
      />
      {actionError ? (
        <div className="px-4 md:px-6">
          <FormError message={actionError} />
        </div>
      ) : null}
      <section aria-labelledby="affected" className="grid gap-2 px-4 md:px-6">
        <h2 id="affected" className="text-sm font-semibold">
          Affected services
        </h2>
        <ul className="flex flex-wrap gap-2">
          {incident.services.map((s) => (
            <li key={s.id} className="rounded-sm border border-border px-2 py-1 font-mono text-[13px]">
              {s.name}
            </li>
          ))}
        </ul>
      </section>
      <section aria-labelledby="detections" className="grid grid-cols-[minmax(0,1fr)] gap-2">
        <h2 id="detections" className="px-4 text-sm font-semibold md:px-6">
          Detections ({incident.detections.length})
        </h2>
        <ul className="grid border-b border-rule">
          {incident.detections.map((item) => (
            <DetectionCard key={item.detection.id} item={item} opener={item.detection.id === openerId} />
          ))}
        </ul>
      </section>
      <section aria-labelledby="timeline" className="grid grid-cols-[minmax(0,1fr)] gap-2">
        <h2 id="timeline" className="px-4 text-sm font-semibold md:px-6">
          Timeline
        </h2>
        {events.isPending ? (
          <div className="px-4 md:px-6">
            <SkeletonBars rows={2} label="timeline" />
          </div>
        ) : events.isError ? (
          <div className="px-4 md:px-6">
            <ErrorBlock what="the timeline" error={errorInfo(events.error)} onRetry={() => void events.refetch()} />
          </div>
        ) : (
          <Timeline events={allEvents} />
        )}
        {events.hasNextPage ? (
          <div className="px-4 md:px-6">
            <Button variant="outline" className="h-8" onClick={() => void events.fetchNextPage()} disabled={events.isFetchingNextPage}>
              Load more events
            </Button>
          </div>
        ) : null}
      </section>
    </div>
  );
}
