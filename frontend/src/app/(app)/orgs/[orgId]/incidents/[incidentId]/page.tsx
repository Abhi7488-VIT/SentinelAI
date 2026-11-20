import type { Metadata } from "next";

import { IncidentDetailView } from "@/components/incidents/incident-detail-view";

export const metadata: Metadata = { title: "Incident" };

export default function IncidentPage() {
  return <IncidentDetailView />;
}
