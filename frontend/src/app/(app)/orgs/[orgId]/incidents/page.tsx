import type { Metadata } from "next";
import { Suspense } from "react";

import { SkeletonBars } from "@/components/common/states";
import { IncidentsView } from "@/components/incidents/incidents-view";

export const metadata: Metadata = { title: "Incidents" };

export default function IncidentsPage() {
  // The view reads its filters from the URL (useSearchParams), so it renders inside a boundary.
  return (
    <Suspense
      fallback={
        <div className="p-6">
          <SkeletonBars label="incidents" />
        </div>
      }
    >
      <IncidentsView />
    </Suspense>
  );
}
