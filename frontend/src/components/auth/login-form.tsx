"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { FormError, FormField } from "@/components/form-field";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError } from "@/lib/api/client";

const schema = z.object({
  email: z.email("Enter a valid email address."),
  password: z.string().min(1, "Enter your password."),
});

export type LoginValues = z.infer<typeof schema>;

export function messageFor(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "rate_limited") return "Too many attempts. Wait a minute and try again.";
    return error.message;
  }
  return "Something went wrong. Try again.";
}

export function LoginForm({ onSubmit }: { onSubmit: (values: LoginValues) => Promise<void> }) {
  const [serverError, setServerError] = useState<string | null>(null);
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<LoginValues>({ resolver: zodResolver(schema) });

  const submit = handleSubmit(async (values) => {
    setServerError(null);
    try {
      await onSubmit(values);
    } catch (error) {
      setServerError(messageFor(error));
    }
  });

  return (
    <form onSubmit={submit} noValidate className="grid gap-4">
      <FormError message={serverError} />
      <FormField label="Email" error={errors.email?.message}>
        {(a11y) => (
          <Input {...a11y} type="email" autoComplete="email" className="h-10" {...register("email")} />
        )}
      </FormField>
      <FormField label="Password" error={errors.password?.message}>
        {(a11y) => (
          <Input
            {...a11y}
            type="password"
            autoComplete="current-password"
            className="h-10"
            {...register("password")}
          />
        )}
      </FormField>
      <Button type="submit" className="mt-2 h-10" disabled={isSubmitting}>
        {isSubmitting ? "Signing in…" : "Sign in"}
      </Button>
    </form>
  );
}
