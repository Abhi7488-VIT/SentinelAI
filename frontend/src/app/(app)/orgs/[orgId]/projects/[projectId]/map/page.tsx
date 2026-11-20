import type { Metadata } from "next";

import { ServiceMapView } from "@/components/service-map/service-map-view";

export const metadata: Metadata = { title: "Service map" };

export default function ServiceMapPage() {
  return <ServiceMapView />;
}
