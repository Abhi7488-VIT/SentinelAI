"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useState } from "react";

import { CreateProjectDialog } from "@/components/projects/create-project-dialog";
import { projectsKey } from "@/components/projects/keys";
import { type ListState, ProjectsList } from "@/components/projects/projects-list";
import { useOrg } from "@/components/shell/org-context";
import { Button } from "@/components/ui/button";
import { ApiError, api, unwrap } from "@/lib/api/client";
import { roleAtLeast } from "@/lib/api/types";

const PAGE_SIZE = 25;



function subline(count: number, more: boolean, canCreate: boolean): string {
  if (count === 0) return canCreate ? "Nothing is being watched yet." : "Nothing is being watched yet. You have view access.";
  const n = more ? `${count}+` : String(count);
  const noun = count === 1 && !more ? "project" : "projects";
  return `${n} ${noun}${canCreate ? "" : " · you have view access"}`;
}

export function ProjectsView() {
  const { orgId, membership } = useOrg();
  const canCreate = roleAtLeast(membership.role, "engineer");
  const [creating, setCreating] = useState(false);

  const query = useInfiniteQuery({
    queryKey: projectsKey(orgId),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/api/v1/orgs/{org_id}/projects", {
          params: { path: { org_id: orgId }, query: { limit: PAGE_SIZE, cursor: pageParam } },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

  let state: ListState;
  if (query.isPending) state = { kind: "loading" };
  else if (query.isError) {
    const err = query.error;
    state = {
      kind: "error",
      message: err instanceof ApiError ? err.message : "Unexpected error.",
      requestId: err instanceof ApiError ? err.requestId : null,
    };
  } else state = { kind: "ready", projects: query.data.pages.flatMap((p) => p.items) };

  const count = state.kind === "ready" ? state.projects.length : 0;

  return (
    <div className="grid gap-6 pt-6 pb-10">
      <div className="flex flex-wrap items-start justify-between gap-4 px-4 md:px-6">
        <div className="grid gap-1">
          <h1 className="text-[22px] leading-7 font-semibold">Projects</h1>
          <p className="text-sm text-muted-foreground" aria-live="polite">
            {state.kind === "ready"
              ? subline(count, Boolean(query.hasNextPage), canCreate)
              : `${membership.org_name}`}
          </p>
        </div>
        {canCreate ? (
          <Button className="h-9 gap-1.5" onClick={() => setCreating(true)}>
            <Plus className="size-4" aria-hidden />
            New project
          </Button>
        ) : null}
      </div>
      <ProjectsList
        state={state}
        canCreate={canCreate}
        onCreate={() => setCreating(true)}
        onRetry={() => void query.refetch()}
        hasMore={Boolean(query.hasNextPage)}
        loadingMore={query.isFetchingNextPage}
        onLoadMore={() => void query.fetchNextPage()}
        hrefFor={(p) => `/orgs/${orgId}/projects/${p.id}/services`}
      />
      {canCreate ? <CreateProjectDialog orgId={orgId} open={creating} onOpenChange={setCreating} /> : null}
    </div>
  );
}
