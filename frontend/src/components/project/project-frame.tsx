"use client";

/*
 * Frame for a project's pages: project name, then tabs (Overview · Services · Service map).
 * Loads the project once; children read it from ProjectContext.
 */
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useParams, usePathname } from "next/navigation";
import { createContext, useContext } from "react";

import { ErrorBlock, errorInfo } from "@/components/common/states";
import { useOrg } from "@/components/shell/org-context";
import { api, unwrap } from "@/lib/api/client";
import { keys } from "@/lib/api/keys";
import type { Project } from "@/lib/api/types";

const ProjectContext = createContext<Project | null>(null);

export function useProject(): Project {
  const project = useContext(ProjectContext);
  if (!project) throw new Error("useProject outside ProjectFrame");
  return project;
}

const TABS = [
  { segment: "", label: "Overview" },
  { segment: "/services", label: "Services" },
  { segment: "/map", label: "Service map" },
] as const;

export function ProjectFrame({ children }: { children: React.ReactNode }) {
  const { orgId } = useOrg();
  const { projectId } = useParams<{ projectId: string }>();
  const pathname = usePathname();
  const query = useQuery({
    queryKey: keys.project(orgId, projectId),
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/orgs/{org_id}/projects/{project_id}", {
          params: { path: { org_id: orgId, project_id: projectId } },
        }),
      ),
  });
  const base = `/orgs/${orgId}/projects/${projectId}`;

  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-6 pt-6 pb-10">
      <div className="grid gap-3 px-4 md:px-6">
        <p className="text-xs text-muted-foreground">
          <Link href={`/orgs/${orgId}/projects`} className="hover:underline">
            Projects
          </Link>{" "}
          /
        </p>
        <h1 className="min-h-7 text-[22px] leading-7 font-semibold">
          {query.data?.name ?? (query.isPending ? <span className="sr-only">Loading project…</span> : "Project")}
        </h1>
        <nav aria-label="Project" className="-mx-1 flex gap-1 overflow-x-auto border-b border-rule">
          {TABS.map((t) => {
            const href = `${base}${t.segment}`;
            const active = t.segment === "" ? pathname === base : pathname.startsWith(href);
            return (
              <Link
                key={t.label}
                href={href}
                aria-current={active ? "page" : undefined}
                className={`-mb-px flex h-9 items-center border-b-2 px-2 text-sm whitespace-nowrap ${active ? "border-foreground font-medium" : "border-transparent text-muted-foreground hover:text-foreground"}`}
              >
                {t.label}
              </Link>
            );
          })}
        </nav>
      </div>
      {query.isError ? (
        <div className="px-4 md:px-6">
          <ErrorBlock what="the project" error={errorInfo(query.error)} onRetry={() => void query.refetch()} />
        </div>
      ) : query.data ? (
        <ProjectContext.Provider value={query.data}>{children}</ProjectContext.Provider>
      ) : null}
    </div>
  );
}
