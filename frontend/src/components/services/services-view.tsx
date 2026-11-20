"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useState } from "react";

import { errorInfo } from "@/components/common/states";
import { useProject } from "@/components/project/project-frame";
import { ApiKeysDialog } from "@/components/services/api-keys-dialog";
import { ArchiveServiceDialog, CreateServiceDialog, DependenciesDialog } from "@/components/services/service-dialogs";
import { type ServicesState, ServicesList } from "@/components/services/services-list";
import { useOrg } from "@/components/shell/org-context";
import { Button } from "@/components/ui/button";
import { api, unwrap } from "@/lib/api/client";
import { keys, POLL_MS } from "@/lib/api/keys";
import { roleAtLeast, type Service } from "@/lib/api/types";

type Open = { kind: "create" } | { kind: "deps" | "keys" | "archive"; service: Service } | null;

export function ServicesView() {
  const { orgId, membership } = useOrg();
  const project = useProject();
  const canEdit = roleAtLeast(membership.role, "engineer") && !project.archived_at;
  const canAdmin = roleAtLeast(membership.role, "admin") && !project.archived_at;
  const [open, setOpen] = useState<Open>(null);

  const query = useInfiniteQuery({
    queryKey: keys.services(orgId, project.id),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/api/v1/orgs/{org_id}/services", {
          params: { path: { org_id: orgId }, query: { project_id: project.id, limit: 100, cursor: pageParam } },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: POLL_MS,
  });

  const services = query.data?.pages.flatMap((p) => p.items) ?? [];
  const state: ServicesState = query.isPending
    ? { kind: "loading" }
    : query.isError
      ? { kind: "error", error: errorInfo(query.error) }
      : { kind: "ready", services };
  const close = () => setOpen(null);

  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3 px-4 md:px-6">
        <p className="text-sm text-muted-foreground" aria-live="polite">
          {state.kind === "ready" ? `${services.length}${query.hasNextPage ? "+" : ""} active services` : " "}
          {canAdmin ? "" : " · ingest keys need the admin role"}
        </p>
        {canEdit ? (
          <Button className="h-9 gap-1.5" onClick={() => setOpen({ kind: "create" })}>
            <Plus className="size-4" aria-hidden />
            New service
          </Button>
        ) : null}
      </div>
      <ServicesList
        state={state}
        onRetry={() => void query.refetch()}
        onCreate={canEdit ? () => setOpen({ kind: "create" }) : undefined}
        actions={{
          canEdit,
          canAdmin,
          onDependencies: (service) => setOpen({ kind: "deps", service }),
          onKeys: (service) => setOpen({ kind: "keys", service }),
          onArchive: (service) => setOpen({ kind: "archive", service }),
        }}
      />
      {query.hasNextPage ? (
        <div className="px-4 md:px-6">
          <Button variant="outline" className="h-8" onClick={() => void query.fetchNextPage()} disabled={query.isFetchingNextPage}>
            {query.isFetchingNextPage ? "Loading…" : "Load more"}
          </Button>
        </div>
      ) : null}
      <CreateServiceDialog
        orgId={orgId}
        projectId={project.id}
        open={open?.kind === "create"}
        onOpenChange={(o) => setOpen(o ? { kind: "create" } : null)}
      />
      {open?.kind === "deps" ? (
        <DependenciesDialog orgId={orgId} projectId={project.id} service={open.service} candidates={services} onClose={close} />
      ) : null}
      {open?.kind === "keys" ? <ApiKeysDialog orgId={orgId} service={open.service} onClose={close} /> : null}
      {open?.kind === "archive" ? (
        <ArchiveServiceDialog orgId={orgId} projectId={project.id} service={open.service} onClose={close} />
      ) : null}
    </div>
  );
}
