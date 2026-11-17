"use client";

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
import { projectsKey } from "@/components/projects/keys";

const SLUG = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

const schema = z.object({
  name: z.string().trim().min(1, "Name the project.").max(100, "Use at most 100 characters."),
  slug: z
    .string()
    .trim()
    .refine((v) => v === "" || SLUG.test(v), "Lowercase letters, digits and hyphens; no hyphen at either end."),
  description: z.string().trim().max(2000, "Use at most 2,000 characters."),
});

type Values = z.infer<typeof schema>;

export function CreateProjectDialog({
  orgId,
  open,
  onOpenChange,
}: {
  orgId: string;
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
  } = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { name: "", slug: "", description: "" } });

  const create = useMutation({
    mutationFn: (values: Values) =>
      unwrap(
        api.POST("/api/v1/orgs/{org_id}/projects", {
          params: { path: { org_id: orgId } },
          body: {
            name: values.name,
            slug: values.slug || null,
            description: values.description || null,
          },
        }),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: projectsKey(orgId) });
      reset();
      onOpenChange(false);
    },
    onError: (error) => {
      if (error instanceof ApiError && error.code === "slug_taken") {
        setError("slug", { message: "Another project in this organization uses this slug." });
        return;
      }
      setServerError(messageFor(error));
    },
  });

  const submit = handleSubmit((values) => {
    setServerError(null);
    create.mutate(values);
  });

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          reset();
          setServerError(null);
        }
        onOpenChange(next);
      }}
    >
      <DialogContent className="gap-6 p-6 sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="text-base font-semibold">New project</DialogTitle>
          <DialogDescription>A service or system SentinelAI should watch.</DialogDescription>
        </DialogHeader>
        <form id="create-project" onSubmit={submit} noValidate className="grid gap-4">
          <FormError message={serverError} />
          <FormField label="Name" error={errors.name?.message}>
            {(a11y) => <Input {...a11y} className="h-10" autoFocus {...register("name")} />}
          </FormField>
          <FormField
            label="Slug"
            optional
            hint="Used in URLs and the API. Leave empty to derive it from the name."
            error={errors.slug?.message}
          >
            {(a11y) => (
              <Input {...a11y} className="h-10 font-mono" spellCheck={false} {...register("slug")} />
            )}
          </FormField>
          <FormField label="Description" optional error={errors.description?.message}>
            {(a11y) => <Textarea {...a11y} rows={3} {...register("description")} />}
          </FormField>
        </form>
        <DialogFooter className="-mx-6 -mb-6 px-6 py-4">
          <Button variant="outline" className="h-9" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button type="submit" form="create-project" className="h-9" disabled={create.isPending}>
            {create.isPending ? "Creating…" : "Create project"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
