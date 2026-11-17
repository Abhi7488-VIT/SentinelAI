"use client";

import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";

import { api, refreshAccessToken, tokenStore, unwrap } from "@/lib/api/client";
import type { Me } from "@/lib/api/types";

type Status = "loading" | "authenticated" | "anonymous";

export interface RegisterInput {
  email: string;
  password: string;
  display_name: string;
  org_name: string;
}

interface AuthContextValue {
  status: Status;
  me: Me | null;
  login: (email: string, password: string) => Promise<Me>;
  register: (input: RegisterInput) => Promise<Me>;
  logout: () => Promise<void>;
  reloadMe: () => Promise<Me | null>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

async function fetchMe(): Promise<Me> {
  return unwrap(api.GET("/api/v1/auth/me"));
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<Status>("loading");
  const [me, setMe] = useState<Me | null>(null);
  const bootstrapped = useRef(false);

  const signedOut = useCallback(() => {
    tokenStore.set(null);
    setMe(null);
    setStatus("anonymous");
    queryClient.clear();
  }, [queryClient]);

  const reloadMe = useCallback(async () => {
    try {
      const next = await fetchMe();
      setMe(next);
      setStatus("authenticated");
      return next;
    } catch {
      signedOut();
      return null;
    }
  }, [signedOut]);

  // Restore the session from the refresh cookie once per page load.
  useEffect(() => {
    if (bootstrapped.current) return;
    bootstrapped.current = true;
    void (async () => {
      const token = await refreshAccessToken();
      if (token) await reloadMe();
      else setStatus("anonymous");
    })();
  }, [reloadMe]);

  // If a refresh fails later (session revoked or expired), drop to signed-out.
  useEffect(
    () =>
      tokenStore.subscribe(() => {
        if (tokenStore.get() === null) {
          setMe(null);
          setStatus((s) => (s === "loading" ? s : "anonymous"));
        }
      }),
    [],
  );

  const login = useCallback(async (email: string, password: string) => {
    const res = await unwrap(api.POST("/api/v1/auth/login", { body: { email, password } }));
    tokenStore.set(res.access_token);
    const next = await fetchMe();
    setMe(next);
    setStatus("authenticated");
    return next;
  }, []);

  const register = useCallback(async (input: RegisterInput) => {
    const res = await unwrap(api.POST("/api/v1/auth/register", { body: input }));
    tokenStore.set(res.access_token);
    const next = await fetchMe();
    setMe(next);
    setStatus("authenticated");
    return next;
  }, []);

  const logout = useCallback(async () => {
    try {
      await unwrap(api.POST("/api/v1/auth/logout"));
    } finally {
      signedOut();
    }
  }, [signedOut]);

  const value = useMemo(
    () => ({ status, me, login, register, logout, reloadMe }),
    [status, me, login, register, logout, reloadMe],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}

/** Where a signed-in user lands: their first organization's projects. */
export function homePath(me: Me | null): string {
  const first = me?.memberships[0];
  return first ? `/orgs/${first.org_id}/projects` : "/no-organization";
}
