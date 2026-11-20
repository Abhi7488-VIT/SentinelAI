"use client";

/* Create service, edit declared dependencies, archive. Each mutation invalidates the services list. */
import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { messageFor } from "@/components/auth/login-form";
import { FormError, FormField } from "@/components/form-field";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ApiError, api, unwrap } from "@/lib/api/client";
import { keys } from "@/lib/api/keys";
import type { DependencyIn, Service } from "@/lib/api/types";

// Same patterns as the backend (app/models/service.py).
const SERVICE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const ENVIRONMENT = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
const EXTERNAL_TARGET = /^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,254}$/;

const createSchema = z.object({
  name: z.string().trim().regex(SERVICE_NAME, "Letters, digits, '.', '_' and '-'; start with a letter or digit."),
  environment: z.string().trim().regex(ENVIRONMENT, "Lowercase letters, digits and hyphens."),
  description: z.string().trim().max(2000, "Use at most 2,000 characters."),
});

type CreateValues = z.infer<typeof createSchema>;

export function CreateServiceDialog({
  orgId,
  projectId,
  open,
  onOpenChange,
}: {
  orgId: string;
  projectId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const [serverError, setServerError] = useState<string | null>(null);
  const {
    register,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: { name: "", environment: "production", description: "" },
  });
  const create = useMutation({
    mutationFn: (v: CreateValues) =>
      unwrap(
        api.POST("/api/v1/orgs/{org_id}/services", {
          params: { path: { org_id: orgId } },
          body: { project_id: projectId, name: v.name, environment: v.environment, description: v.description || null },
        }),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: keys.services(orgId, projectId) });
      reset();
      onOpenChange(false);
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        setError("name", { message: error.message });
        return;
      }
      setServerError(messageFor(error));
    },
  });
  const close = (next: boolean) => {
    if (!next) {
      reset();
      setServerError(null);
    }
    onOpenChange(next);
  };
  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="gap-6 p-6 sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-base font-semibold">New service</DialogTitle>
          <DialogDescription>
            Use the name the service reports as <code className="font-mono">service.name</code>.
          </DialogDescription>
        </DialogHeader>
        <form
          id="create-service"
          noValidate
          className="grid gap-4"
          onSubmit={handleSubmit((v) => {
            setServerError(null);
            create.mutate(v);
          })}
        >
          <FormError message={serverError} />
          <FormField label="Name" error={errors.name?.message}>
            {(a11y) => <Input {...a11y} className="h-10 font-mono" autoFocus spellCheck={false} {...register("name")} />}
          </FormField>
          <FormField label="Environment" error={errors.environment?.message}>
            {(a11y) => <Input {...a11y} className="h-10 font-mono" spellCheck={false} {...register("environment")} />}
          </FormField>
          <FormField label="Description" optional error={errors.description?.message}>
            {(a11y) => <Textarea {...a11y} rows={2} {...register("description")} />}
          </FormField>
        </form>
        <DialogFooter className="-mx-6 -mb-6 px-6 py-4">
          <Button variant="outline" className="h-9" onClick={() => close(false)}>
            Cancel
          </Button>
          <Button type="submit" form="create-service" className="h-9" disabled={create.isPending}>
            {create.isPending ? "Creating…" : "Create service"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function DependenciesDialog({
  orgId,
  projectId,
  service,
  candidates,
  onClose,
}: {
  orgId: string;
  projectId: string;
  service: Service;
  candidates: Service[];
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(service.dependencies.flatMap((d) => (d.target_service_id ? [d.target_service_id] : []))),
  );
  const [externals, setExternals] = useState(
    service.dependencies.flatMap((d) => (d.target_external ? [d.target_external] : [])).join("\n"),
  );
  const [error, setError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: (dependencies: DependencyIn[]) =>
      unwrap(
        api.PUT("/api/v1/orgs/{org_id}/services/{service_id}/dependencies", {
          params: { path: { org_id: orgId, service_id: service.id } },
          body: { dependencies },
        }),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: keys.services(orgId, projectId) });
      await queryClient.invalidateQueries({ queryKey: keys.graph(orgId, projectId) });
      onClose();
    },
    onError: (e) => setError(messageFor(e)),
  });
  const submit = () => {
    const ext = externals
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
    const bad = ext.find((e) => !EXTERNAL_TARGET.test(e));
    if (bad) {
      setError(`"${bad}" is not a valid external target (e.g. postgresql:orders).`);
      return;
    }
    setError(null);
    save.mutate([
      ...[...selected].map((id) => ({ target_service_id: id })),
      ...ext.map((name) => ({ target_external: name })),
    ]);
  };
  const others = candidates.filter((c) => c.id !== service.id);
  return (
    <Dialog open onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent className="gap-6 p-6 sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-base font-semibold">Dependencies of {service.name}</DialogTitle>
          <DialogDescription>
            Declared calls. Observed calls are found from traces on their own and shown on the service map.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-4">
          <FormError message={error} />
          <fieldset className="grid gap-2">
            <legend className="mb-2 text-sm font-medium">Services in this project</legend>
            {others.length === 0 ? <p className="text-sm text-muted-foreground">No other services.</p> : null}
            {others.map((c) => (
              <label key={c.id} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  className="size-4"
                  checked={selected.has(c.id)}
                  onChange={(e) => {
                    const next = new Set(selected);
                    if (e.target.checked) next.add(c.id);
                    else next.delete(c.id);
                    setSelected(next);
                  }}
                />
                <span className="font-mono">{c.name}</span>
              </label>
            ))}
          </fieldset>
          <FormField label="External dependencies" optional hint="One per line, e.g. postgresql:orders or redis:sessions.">
            {(a11y) => (
              <Textarea {...a11y} rows={3} className="font-mono" value={externals} onChange={(e) => setExternals(e.target.value)} />
            )}
          </FormField>
        </div>
        <DialogFooter className="-mx-6 -mb-6 px-6 py-4">
          <Button variant="outline" className="h-9" onClick={onClose}>
            Cancel
          </Button>
          <Button className="h-9" onClick={submit} disabled={save.isPending}>
            {save.isPending ? "Saving…" : "Save dependencies"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function ArchiveServiceDialog({
  orgId,
  projectId,
  service,
  onClose,
}: {
  orgId: string;
  projectId: string;
  service: Service;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const archive = useMutation({
    mutationFn: () =>
      unwrap(
        api.DELETE("/api/v1/orgs/{org_id}/services/{service_id}", {
          params: { path: { org_id: orgId, service_id: service.id } },
        }),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: keys.services(orgId, projectId) });
      onClose();
    },
    onError: (e) => setError(messageFor(e)),
  });
  return (
    <Dialog open onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent className="gap-6 p-6 sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-base font-semibold">Archive {service.name}?</DialogTitle>
          <DialogDescription>
            Its ingest keys are revoked now, so telemetry from it is refused. Stored telemetry and incidents stay.
          </DialogDescription>
        </DialogHeader>
        <FormError message={error} />
        <DialogFooter className="-mx-6 -mb-6 px-6 py-4">
          <Button variant="outline" className="h-9" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="destructive" className="h-9" onClick={() => archive.mutate()} disabled={archive.isPending}>
            {archive.isPending ? "Archiving…" : "Archive service"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
