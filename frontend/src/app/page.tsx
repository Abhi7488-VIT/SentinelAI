"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { ProductMark } from "@/components/product-mark";
import { homePath, useAuth } from "@/lib/auth/auth-provider";

/** Entry: send signed-in users to their first organization, everyone else to sign in. */
export default function Home() {
  const { status, me } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (status === "authenticated") router.replace(homePath(me));
    if (status === "anonymous") router.replace("/login");
  }, [status, me, router]);

  return (
    <main className="grid min-h-screen place-items-center" aria-busy="true">
      <h1 className="sr-only">SentinelAI</h1>
      <ProductMark size={28} />
    </main>
  );
}
