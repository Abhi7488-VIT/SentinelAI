import Link from "next/link";

import { ProductName } from "@/components/product-mark";

/** Sign-in screens: the product on the frame ground, the form on the white sheet. */
export function AuthFrame({
  title,
  children,
  footer,
}: {
  title: string;
  children: React.ReactNode;
  footer: React.ReactNode;
}) {
  return (
    <main className="mx-auto grid min-h-screen w-full max-w-5xl content-center gap-10 px-4 py-10 md:grid-cols-[1fr_400px] md:items-center md:gap-16 md:px-8">
      <section className="grid gap-4">
        <Link href="/" className="w-fit">
          <ProductName className="text-base" />
        </Link>
        <p className="max-w-md text-[22px] leading-8 font-semibold text-foreground">
          Find out why production broke, with the evidence attached.
        </p>
        <p className="max-w-md text-sm text-muted-foreground">
          SentinelAI groups failures into incidents, investigates root causes against your
          telemetry, and proposes fixes for a human to approve.
        </p>
      </section>
      <section className="rounded-[var(--radius-sheet)] border border-border bg-background p-6 md:p-8">
        <h1 className="mb-6 text-[22px] leading-7 font-semibold">{title}</h1>
        {children}
        <div className="mt-6 text-sm text-muted-foreground">{footer}</div>
      </section>
    </main>
  );
}
