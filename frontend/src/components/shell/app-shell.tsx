"use client";

/*
 * Framed-sheet shell.
 * Desktop (>= 1024px): grid [228px sidebar | 1fr] x [48px top bar | 1fr]. Sidebar and top bar
 * share the tinted frame ground with no rule between them; the page is a white sheet inset 8px
 * from the right and bottom window edges (radius 16, 1px edge, no shadow).
 * Phone: the frame disappears; a 48px bar holds the menu button and the screen name, and the
 * navigation opens as a sheet.
 */
import { ChevronsUpDown, FolderKanban, LogOut, Menu } from "lucide-react";
import Link from "next/link";
import { useParams, usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { ProductName } from "@/components/product-mark";
import { OrgContext } from "@/components/shell/org-context";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import type { Me, Membership } from "@/lib/api/types";
import { useAuth } from "@/lib/auth/auth-provider";

const NAV = [{ segment: "projects", label: "Projects", icon: FolderKanban }] as const;

function roleLabel(role: Membership["role"]): string {
  return role.charAt(0).toUpperCase() + role.slice(1);
}

function OrgSwitcher({ me, current }: { me: Me; current: Membership | undefined }) {
  const router = useRouter();
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <button
            type="button"
            className="flex h-12 w-full items-center gap-2 rounded-md px-2 text-left hover:bg-active focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          />
        }
        aria-label="Switch organization"
      >
        <span className="grid min-w-0 flex-1">
          <span className="truncate text-sm font-medium">{current?.org_name ?? "Organization"}</span>
          <span className="truncate text-xs text-muted-foreground">
            {current ? roleLabel(current.role) : "Not a member"}
          </span>
        </span>
        <ChevronsUpDown className="size-4 text-muted-foreground" aria-hidden />
      </DropdownMenuTrigger>
      <DropdownMenuContent className="w-60">
        <DropdownMenuGroup>
          <DropdownMenuLabel className="text-xs text-muted-foreground">Organizations</DropdownMenuLabel>
          {me.memberships.map((m) => (
            <DropdownMenuItem
              key={m.org_id}
              onClick={() => router.push(`/orgs/${m.org_id}/projects`)}
              className="flex justify-between gap-3"
            >
              <span className="truncate">{m.org_name}</span>
              <span className="text-xs text-muted-foreground">{roleLabel(m.role)}</span>
            </DropdownMenuItem>
          ))}
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function NavLinks({ orgId, onNavigate }: { orgId: string; onNavigate?: () => void }) {
  const pathname = usePathname();
  return (
    <nav aria-label="Main" className="grid gap-0.5">
      {NAV.map(({ segment, label, icon: Icon }) => {
        const href = `/orgs/${orgId}/${segment}`;
        const active = pathname.startsWith(href);
        return (
          <Link
            key={segment}
            href={href}
            onClick={onNavigate}
            aria-current={active ? "page" : undefined}
            className={`flex h-8 items-center gap-2.5 rounded-md px-2 text-sm text-foreground hover:bg-active focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none ${active ? "bg-active" : ""}`}
          >
            <Icon className="size-4 text-muted-foreground" strokeWidth={1.5} aria-hidden />
            {label}
          </Link>
        );
      })}
    </nav>
  );
}

function AccountMenu({ me, onSignOut }: { me: Me; onSignOut: () => void }) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={<Button variant="ghost" className="h-8 gap-2 px-2 text-sm font-normal" />}
        aria-label="Account"
      >
        <span
          aria-hidden
          className="grid size-6 place-items-center rounded-full bg-pill-neutral text-xs font-medium text-pill-neutral-ink"
        >
          {me.user.display_name.charAt(0).toUpperCase()}
        </span>
        <span className="max-w-40 truncate">{me.user.display_name}</span>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-60">
        <DropdownMenuGroup>
          <DropdownMenuLabel className="grid font-normal">
            <span className="truncate text-sm">{me.user.display_name}</span>
            <span className="truncate text-xs text-muted-foreground">{me.user.email}</span>
          </DropdownMenuLabel>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={onSignOut}>
          <LogOut className="size-4" aria-hidden />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function OrgNotFound() {
  return (
    <div className="grid gap-2 p-6">
      <h1 className="text-[22px] leading-7 font-semibold">Organization not found</h1>
      <p className="text-sm text-muted-foreground">
        It doesn&apos;t exist, or you aren&apos;t a member of it. Pick one of your organizations
        from the switcher.
      </p>
    </div>
  );
}

/** The shell's frame at its final size, shown while the session is restored. */
export function ShellFallback() {
  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[var(--sidebar-width)_1fr] lg:grid-rows-[48px_1fr]" aria-busy="true">
      <div className="hidden px-3 lg:row-span-2 lg:block">
        <div className="flex h-12 items-center px-2">
          <ProductName />
        </div>
      </div>
      <div className="h-12" />
      <div className="mx-0 min-h-[calc(100vh-48px)] bg-background lg:mr-[var(--sheet-inset)] lg:mb-[var(--sheet-inset)] lg:min-h-[calc(100vh-56px)] lg:rounded-[var(--radius-sheet)] lg:border lg:border-border" />
    </div>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const { orgId } = useParams<{ orgId: string }>();
  const pathname = usePathname();
  const router = useRouter();
  const { status, me, logout } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    if (status === "anonymous") router.replace("/login");
  }, [status, router]);

  if (status !== "authenticated" || !me) return <ShellFallback />;

  const membership = me.memberships.find((m) => m.org_id === orgId);
  const section = NAV.find((n) => pathname.includes(`/${n.segment}`))?.label ?? "";
  const signOut = () => {
    void logout().then(() => router.replace("/login"));
  };

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[var(--sidebar-width)_1fr] lg:grid-rows-[48px_1fr]">
      {/* Sidebar (desktop) */}
      <aside className="hidden flex-col gap-3 px-3 pb-3 lg:row-span-2 lg:flex">
        <div className="flex h-12 items-center px-2">
          <ProductName />
        </div>
        <OrgSwitcher me={me} current={membership} />
        {membership ? <NavLinks orgId={orgId} /> : null}
      </aside>

      {/* Top bar (desktop): where you are, and the account */}
      <header className="hidden h-12 items-center justify-between pr-[calc(var(--sheet-inset)+16px)] pl-6 lg:flex">
        <p className="truncate text-sm text-muted-foreground">
          {membership?.org_name ?? "Organization"}
          {section ? <span className="text-foreground"> / {section}</span> : null}
        </p>
        <AccountMenu me={me} onSignOut={signOut} />
      </header>

      {/* Top bar (phone) */}
      <header className="flex h-12 items-center gap-2 px-2 lg:hidden">
        <Sheet open={menuOpen} onOpenChange={setMenuOpen}>
          <SheetTrigger render={<Button variant="ghost" size="icon" className="size-11" />} aria-label="Open menu">
            <Menu className="size-5" aria-hidden />
          </SheetTrigger>
          <SheetContent side="left" className="w-[85%] gap-3 bg-ground p-3">
            <SheetTitle className="flex h-12 items-center px-2">
              <ProductName />
            </SheetTitle>
            <OrgSwitcher me={me} current={membership} />
            {membership ? <NavLinks orgId={orgId} onNavigate={() => setMenuOpen(false)} /> : null}
            <div className="mt-auto grid gap-1 px-2">
              <span className="truncate text-sm">{me.user.display_name}</span>
              <span className="truncate text-xs text-muted-foreground">{me.user.email}</span>
              <Button variant="outline" className="mt-2 h-11" onClick={signOut}>
                <LogOut className="size-4" aria-hidden />
                Sign out
              </Button>
            </div>
          </SheetContent>
        </Sheet>
        <span className="truncate text-sm font-medium">{section || "SentinelAI"}</span>
      </header>

      {/* The sheet */}
      <main className="min-h-[calc(100vh-48px)] bg-background lg:mr-[var(--sheet-inset)] lg:mb-[var(--sheet-inset)] lg:min-h-[calc(100vh-56px)] lg:rounded-[var(--radius-sheet)] lg:border lg:border-border">
        {membership ? (
          <OrgContext.Provider value={{ orgId, membership }}>{children}</OrgContext.Provider>
        ) : (
          <OrgNotFound />
        )}
      </main>
    </div>
  );
}
