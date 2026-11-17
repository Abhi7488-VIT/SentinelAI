"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { AuthFrame } from "@/components/auth/auth-frame";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/lib/auth/auth-provider";

/** A signed-in user who has left every organization. */
export default function NoOrganizationPage() {
  const { status, logout } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (status === "anonymous") router.replace("/login");
  }, [status, router]);

  return (
    <AuthFrame title="No organization" footer={null}>
      <p className="mb-6 text-sm">
        You aren&apos;t a member of any organization. Ask an admin to add you, or sign in with
        another account.
      </p>
      <Button variant="outline" className="h-10" onClick={() => void logout()}>
        Sign out
      </Button>
    </AuthFrame>
  );
}
