"use client";

/*
 * Ingest keys of one service (admin). A new key's plaintext is in the create response only: it is
 * shown once here, kept in component state (never stored), and gone when the dialog closes.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { messageFor } from "@/components/auth/login-form";
import { Pill } from "@/components/common/pills";
import { ErrorBlock, errorInfo, SkeletonBars } from "@/components/common/states";
import { FormError } from "@/components/form-field";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { api, unwrap } from "@/lib/api/client";
import { keys } from "@/lib/api/keys";
import type { ApiKey, ApiKeyCreated, Service } from "@/lib/api/types";
import { formatAgo, formatDateTime } from "@/lib/format";

export function NewKeyNotice({ created, onDone }: { created: ApiKeyCreated; onDone: () => void }) {
  const [copied, setCopied] = useState(false);
  return (
    <div role="status" className="grid gap-2 rounded-md border border-border bg-raised p-3">
      <p className="text-sm font-medium">Copy this key now. It won&apos;t be shown again.</p>
      <code data-testid="new-api-key" className="block overflow-x-auto rounded-sm bg-background px-2 py-1.5 font-mono text-[13px] break-all">
        {created.key}
      </code>
      <div className="flex gap-2">
        <Button
          variant="outline"
          className="h-8"
          onClick={() => {
            void navigator.clipboard?.writeText(created.key).then(() => setCopied(true));
          }}
        >
          {copied ? "Copied" : "Copy key"}
        </Button>
        <Button variant="ghost" className="h-8" onClick={onDone}>
          Done
        </Button>
      </div>
    </div>
  );
}

function KeyRow({ k, onRevoke, revoking }: { k: ApiKey; onRevoke: () => void; revoking: boolean }) {
  return (
    <li className="flex flex-wrap items-center justify-between gap-2 border-t border-rule py-2">
      <div className="grid min-w-0">
        <span className="truncate text-sm">
          {k.name} <code className="font-mono text-xs text-muted-foreground">{k.prefix}…</code>
        </span>
        <span className="num text-xs text-muted-foreground">
          Created {formatDateTime(k.created_at)} · last used {formatAgo(k.last_used_at)}
        </span>
      </div>
      {k.revoked_at ? (
        <Pill tone="neutral">Revoked</Pill>
      ) : (
        <Button variant="ghost" className="h-8" onClick={onRevoke} disabled={revoking}>
          Revoke
        </Button>
      )}
    </li>
  );
}

export function ApiKeysDialog({ orgId, service, onClose }: { orgId: string; service: Service; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("collector");
  const [created, setCreated] = useState<ApiKeyCreated | null>(null);
  const [error, setError] = useState<string | null>(null);
  const path = { org_id: orgId, service_id: service.id };
  const list = useQuery({
    queryKey: keys.apiKeys(orgId, service.id),
    queryFn: () =>
      unwrap(api.GET("/api/v1/orgs/{org_id}/services/{service_id}/api-keys", { params: { path, query: { limit: 100 } } })),
  });
  const refresh = () => queryClient.invalidateQueries({ queryKey: keys.apiKeys(orgId, service.id) });
  const create = useMutation({
    mutationFn: () =>
      unwrap(api.POST("/api/v1/orgs/{org_id}/services/{service_id}/api-keys", { params: { path }, body: { name: name.trim() } })),
    onSuccess: async (key) => {
      setCreated(key);
      setError(null);
      await refresh();
    },
    onError: (e) => setError(messageFor(e)),
  });
  const revoke = useMutation({
    mutationFn: (keyId: string) =>
      unwrap(
        api.POST("/api/v1/orgs/{org_id}/services/{service_id}/api-keys/{key_id}/revoke", {
          params: { path: { ...path, key_id: keyId } },
        }),
      ),
    onSuccess: refresh,
    onError: (e) => setError(messageFor(e)),
  });
  return (
    <Dialog open onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent className="gap-5 p-6 sm:max-w-lg">
        <DialogHeader>
          <DialogTitle className="text-base font-semibold">Ingest keys · {service.name}</DialogTitle>
          <DialogDescription>
            Send OTLP/HTTP with header <code className="font-mono">Authorization: Bearer &lt;key&gt;</code>. A key only
            accepts telemetry for this service.
          </DialogDescription>
        </DialogHeader>
        <FormError message={error} />
        {created ? (
          <NewKeyNotice created={created} onDone={() => setCreated(null)} />
        ) : (
          <form
            className="flex gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (name.trim()) create.mutate();
            }}
          >
            <Input aria-label="Key name" className="h-9" value={name} maxLength={64} onChange={(e) => setName(e.target.value)} />
            <Button type="submit" className="h-9" disabled={create.isPending || !name.trim()}>
              {create.isPending ? "Creating…" : "Create key"}
            </Button>
          </form>
        )}
        {list.isPending ? (
          <SkeletonBars rows={2} label="keys" />
        ) : list.isError ? (
          <ErrorBlock what="keys" error={errorInfo(list.error)} onRetry={() => void list.refetch()} />
        ) : list.data.items.length === 0 ? (
          <p className="text-sm text-muted-foreground">No keys yet.</p>
        ) : (
          <ul aria-label="Keys" className="grid">
            {list.data.items.map((k) => (
              <KeyRow key={k.id} k={k} onRevoke={() => revoke.mutate(k.id)} revoking={revoke.isPending} />
            ))}
          </ul>
        )}
      </DialogContent>
    </Dialog>
  );
}
