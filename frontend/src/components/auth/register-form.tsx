"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { messageFor } from "@/components/auth/login-form";
import { FormError, FormField } from "@/components/form-field";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError } from "@/lib/api/client";

const schema = z.object({
  display_name: z.string().trim().min(1, "Enter your name.").max(100),
  email: z.email("Enter a valid email address."),
  password: z
    .string()
    .min(12, "Use at least 12 characters.")
    .max(128, "Use at most 128 characters."),
  org_name: z.string().trim().min(1, "Name your organization.").max(100),
});

export type RegisterValues = z.infer<typeof schema>;

export function RegisterForm({ onSubmit }: { onSubmit: (values: RegisterValues) => Promise<void> }) {
  const [serverError, setServerError] = useState<string | null>(null);
  const {
    register,
    handleSubmit,
    setError,
    formState: { errors, isSubmitting },
  } = useForm<RegisterValues>({ resolver: zodResolver(schema) });

  const submit = handleSubmit(async (values) => {
    setServerError(null);
    try {
      await onSubmit(values);
    } catch (error) {
      if (error instanceof ApiError && error.code === "email_taken") {
        setError("email", { message: "An account with this email already exists." });
        return;
      }
      setServerError(messageFor(error));
    }
  });

  return (
    <form onSubmit={submit} noValidate className="grid gap-4">
      <FormError message={serverError} />
      <FormField label="Your name" error={errors.display_name?.message}>
        {(a11y) => <Input {...a11y} autoComplete="name" className="h-10" {...register("display_name")} />}
      </FormField>
      <FormField label="Work email" error={errors.email?.message}>
        {(a11y) => (
          <Input {...a11y} type="email" autoComplete="email" className="h-10" {...register("email")} />
        )}
      </FormField>
      <FormField label="Password" hint="At least 12 characters." error={errors.password?.message}>
        {(a11y) => (
          <Input
            {...a11y}
            type="password"
            autoComplete="new-password"
            className="h-10"
            {...register("password")}
          />
        )}
      </FormField>
      <FormField
        label="Organization"
        hint="Your team's workspace. You'll be its owner."
        error={errors.org_name?.message}
      >
        {(a11y) => <Input {...a11y} autoComplete="organization" className="h-10" {...register("org_name")} />}
      </FormField>
      <Button type="submit" className="mt-2 h-10" disabled={isSubmitting}>
        {isSubmitting ? "Creating account…" : "Create account"}
      </Button>
    </form>
  );
}
