"use client";

import { useQuery } from "@tanstack/react-query";

import { ErrorBlock, errorInfo, SkeletonBars } from "@/components/common/states";
import { OverviewBody } from "@/components/overview/overview-body";
import { useProject } from "@/components/project/project-frame";
import { useOrg } from "@/components/shell/org-context";
import { api, unwrap } from "@/lib/api/client";
import { keys, POLL_MS } from "@/lib/api/keys";

export function OverviewView() {
  const { orgId } = useOrg();
  const project = useProject();
  const query = useQuery({
    queryKey: keys.overview(orgId, project.id),
    queryFn: () =>
      unwrap(api.GET("/api/v1/orgs/{org_id}/overview", { params: { path: { org_id: orgId }, query: { project_id: project.id } } })),
    refetchInterval: POLL_MS,
  });
  if (query.isPending)
    return (
      <div className="px-4 md:px-6">
        <SkeletonBars rows={4} label="overview" />
      </div>
    );
  if (query.isError)
    return (
      <div className="px-4 md:px-6">
        <ErrorBlock what="the overview" error={errorInfo(query.error)} onRetry={() => void query.refetch()} />
      </div>
    );
  return <OverviewBody data={query.data} orgId={orgId} servicesHref={`/orgs/${orgId}/projects/${project.id}/services`} />;
}
