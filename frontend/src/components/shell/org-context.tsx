"use client";

import { createContext, useContext } from "react";

import type { Membership } from "@/lib/api/types";

export interface OrgContextValue {
  orgId: string;
  membership: Membership;
}

export const OrgContext = createContext<OrgContextValue | null>(null);

export function useOrg(): OrgContextValue {
  const ctx = useContext(OrgContext);
  if (!ctx) throw new Error("useOrg must be used inside an organization route");
  return ctx;
}
