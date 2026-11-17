import { Suspense } from "react";

import { AppShell, ShellFallback } from "@/components/shell/app-shell";

/*
 * Org-scoped routes. The session and data live in the browser (access token in memory), so the
 * shell is a client component; it reads the dynamic [orgId] with useParams, which suspends
 * during prerender under Cache Components — hence the boundary, whose fallback is the shell's
 * own frame at its final size.
 */
export default function OrgLayout({ children }: LayoutProps<"/orgs/[orgId]">) {
  return (
    <Suspense fallback={<ShellFallback />}>
      <AppShell>{children}</AppShell>
    </Suspense>
  );
}
