"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { AuthFrame } from "@/components/auth/auth-frame";
import { RegisterForm } from "@/components/auth/register-form";
import { homePath, useAuth } from "@/lib/auth/auth-provider";

export default function RegisterPage() {
  const { status, me, register } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (status === "authenticated") router.replace(homePath(me));
  }, [status, me, router]);

  return (
    <AuthFrame
      title="Create your account"
      footer={
        <>
          Already have an account?{" "}
          <Link href="/login" className="font-medium text-foreground underline underline-offset-4">
            Sign in
          </Link>
        </>
      }
    >
      <RegisterForm
        onSubmit={async (values) => {
          const next = await register(values);
          router.replace(homePath(next));
        }}
      />
    </AuthFrame>
  );
}
