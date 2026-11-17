"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { AuthFrame } from "@/components/auth/auth-frame";
import { LoginForm } from "@/components/auth/login-form";
import { homePath, useAuth } from "@/lib/auth/auth-provider";

export default function LoginPage() {
  const { status, me, login } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (status === "authenticated") router.replace(homePath(me));
  }, [status, me, router]);

  return (
    <AuthFrame
      title="Sign in"
      footer={
        <>
          New to SentinelAI?{" "}
          <Link href="/register" className="font-medium text-foreground underline underline-offset-4">
            Create an account
          </Link>
        </>
      }
    >
      <LoginForm
        onSubmit={async ({ email, password }) => {
          const next = await login(email, password);
          router.replace(homePath(next));
        }}
      />
    </AuthFrame>
  );
}
