/*
 * Shared loading / error / empty blocks so every page reports state the same way:
 *   error -> one sentence, the server's message, the request id, and Retry
 *   empty -> a dashed box with what is missing and, when possible, what to do next
 */
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api/client";

export interface ErrorInfo {
  message: string;
  requestId: string | null;
}

export function errorInfo(error: unknown): ErrorInfo {
  return error instanceof ApiError
    ? { message: error.message, requestId: error.requestId }
    : { message: "Unexpected error.", requestId: null };
}

export function ErrorBlock({ what, error, onRetry }: { what: string; error: ErrorInfo; onRetry?: () => void }) {
  return (
    <div role="alert" className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-border px-4 py-3">
      <span aria-hidden className="size-2 flex-none rounded-full bg-negative" />
      <p className="text-sm">
        Couldn&apos;t load {what}. {error.message}
      </p>
      {error.requestId ? (
        <span className="font-mono text-xs text-muted-foreground">Request {error.requestId.slice(0, 8)}</span>
      ) : null}
      {onRetry ? (
        <Button variant="link" className="h-auto p-0 text-sm" onClick={onRetry}>
          Retry
        </Button>
      ) : null}
    </div>
  );
}

export function EmptyBlock({ children, action }: { children: React.ReactNode; action?: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-dashed border-control px-4 py-3">
      <p className="text-sm text-muted-foreground">{children}</p>
      {action}
    </div>
  );
}

export function SkeletonBars({ rows = 3, label }: { rows?: number; label: string }) {
  return (
    <div aria-busy="true" className="grid gap-2">
      {Array.from({ length: rows }, (_, i) => (
        <span key={i} aria-hidden className="block h-10 rounded-sm bg-raised" />
      ))}
      <p className="sr-only">Loading {label}…</p>
    </div>
  );
}
