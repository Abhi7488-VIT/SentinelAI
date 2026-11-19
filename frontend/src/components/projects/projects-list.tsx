/*
 * The projects list. One component renders all four states so they share one frame:
 *   loading -> the final layout with flat grey bars at the real row pitch
 *   error   -> one sentence inside the block, the request id, and Retry
 *   empty   -> the header stays; one sentence and the create action in a dashed box
 *   data    -> rows: name (+ description, links to the project), slug (mono), created date
 * Desktop rows are a 3-column grid; below 768px each row becomes two lines.
 */
import Link from "next/link";

import type { Project } from "@/lib/api/types";
import { Button } from "@/components/ui/button";

const GRID =
  "md:grid md:grid-cols-[minmax(0,2.2fr)_minmax(0,1.2fr)_112px] md:items-center md:gap-6";

export type ListState =
  | { kind: "loading" }
  | { kind: "error"; message: string; requestId: string | null }
  | { kind: "ready"; projects: Project[] };

export function formatDate(iso: string): string {
  return iso.slice(0, 10); // ISO date: rows are compared, so YYYY-MM-DD, right-aligned
}

function Header() {
  return (
    <div role="presentation" className={`hidden h-9 px-6 text-xs text-muted-foreground ${GRID}`}>
      <span>Project</span>
      <span>Slug</span>
      <span className="text-right">Created</span>
    </div>
  );
}

function Row({ project, href }: { project: Project; href?: string }) {
  return (
    <li className={`border-t border-rule px-4 py-3 md:min-h-14 md:px-6 md:py-2 ${GRID}`}>
      <div className="flex min-w-0 items-baseline justify-between gap-3 md:block">
        <p className="truncate text-sm font-medium">
          {href ? (
            <Link href={href} className="hover:underline focus-visible:underline">
              {project.name}
            </Link>
          ) : (
            project.name
          )}
        </p>
        <span className="num text-xs text-muted-foreground md:hidden">{formatDate(project.created_at)}</span>
        {project.description ? (
          <p className="hidden truncate text-[13px] text-muted-foreground md:block">{project.description}</p>
        ) : null}
      </div>
      <div className="mt-1 flex min-w-0 items-center gap-2 md:mt-0 md:block">
        <code className="block min-w-0 truncate font-mono text-[13px] text-muted-foreground" title={project.slug}>
          {project.slug}
        </code>
      </div>
      <p className="num hidden text-right text-sm text-muted-foreground md:block">
        {formatDate(project.created_at)}
      </p>
    </li>
  );
}

function SkeletonRows() {
  return (
    <ul aria-hidden className="grid">
      {[0, 1, 2].map((i) => (
        <li key={i} className={`border-t border-rule px-4 py-3 md:min-h-14 md:px-6 md:py-2 ${GRID}`}>
          <span className="block h-3.5 w-40 rounded-sm bg-raised" />
          <span className="mt-2 block h-3.5 w-24 rounded-sm bg-raised md:mt-0" />
          <span className="hidden h-3.5 w-20 justify-self-end rounded-sm bg-raised md:block" />
        </li>
      ))}
    </ul>
  );
}

interface ProjectsListProps {
  state: ListState;
  canCreate: boolean;
  onCreate: () => void;
  onRetry: () => void;
  hasMore?: boolean;
  loadingMore?: boolean;
  onLoadMore?: () => void;
  hrefFor?: (project: Project) => string;
}

export function ProjectsList({
  state,
  canCreate,
  onCreate,
  onRetry,
  hasMore = false,
  loadingMore = false,
  onLoadMore,
  hrefFor,
}: ProjectsListProps) {
  if (state.kind === "loading") {
    return (
      <section aria-label="Projects" aria-busy="true">
        <Header />
        <SkeletonRows />
        <p className="sr-only">Loading projects…</p>
      </section>
    );
  }

  if (state.kind === "error") {
    return (
      <section aria-label="Projects" className="px-4 md:px-6">
        <div role="alert" className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-border px-4 py-3">
          <span aria-hidden className="size-2 flex-none rounded-full bg-negative" />
          <p className="text-sm">Couldn&apos;t load projects. {state.message}</p>
          {state.requestId ? (
            <span className="font-mono text-xs text-muted-foreground">Request {state.requestId.slice(0, 8)}</span>
          ) : null}
          <Button variant="link" className="h-auto p-0 text-sm" onClick={onRetry}>
            Retry
          </Button>
        </div>
      </section>
    );
  }

  if (state.projects.length === 0) {
    return (
      <section aria-label="Projects">
        <Header />
        <div className="px-4 md:px-6">
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-dashed border-control px-4 py-3">
            <p className="text-sm text-muted-foreground">
              No projects yet. Add one for each service you want SentinelAI to watch.
            </p>
            {canCreate ? (
              <Button variant="link" className="h-auto p-0 text-sm" onClick={onCreate}>
                New project
              </Button>
            ) : null}
          </div>
        </div>
      </section>
    );
  }

  return (
    <section aria-label="Projects">
      <Header />
      <ul className="grid border-b border-rule">
        {state.projects.map((p) => (
          <Row key={p.id} project={p} href={hrefFor?.(p)} />
        ))}
      </ul>
      {hasMore ? (
        <div className="px-4 pt-4 md:px-6">
          <Button variant="outline" className="h-8" onClick={onLoadMore} disabled={loadingMore}>
            {loadingMore ? "Loading…" : "Load more"}
          </Button>
        </div>
      ) : null}
    </section>
  );
}
