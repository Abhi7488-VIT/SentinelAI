import { randomBytes } from "node:crypto";

import { expect, type Page, test } from "@playwright/test";

/*
 * Phase 3 golden path, in a real browser against the real stack (API, ingest, writer, beat):
 * create project -> service -> ingest key (shown once) -> fixture telemetry through the public
 * OTLP ingest API with that key -> the detection tick opens an incident -> it appears in the
 * list, its detail explains it, and the overview and service map show the service.
 *
 * The fixture is 3 minutes of failing server spans, backdated (the ingest API accepts timestamps
 * from now - 2 h), so the error-rate rule's 60-s minimum duration is already covered when the
 * batch lands. Measured on the dev Codespace: incident ~75 s after the POST.
 */

const PASSWORD = "correct horse battery staple";
const INGEST_URL = process.env.E2E_INGEST_URL ?? "http://localhost:8001";
const run = Date.now().toString(36);

function field(page: Page, label: string | RegExp) {
  return page.getByLabel(label, { exact: true }).filter({ visible: true });
}

function failingSpans(service: string) {
  const MS = BigInt(1_000_000); // ns per ms (no bigint literals: the TS target is below ES2020)
  const now = BigInt(Date.now()) * MS;
  const spans = Array.from({ length: 360 }, (_, i) => {
    const start = now - BigInt(180_000 - i * 500) * MS;
    return {
      traceId: randomBytes(16).toString("hex"),
      spanId: randomBytes(8).toString("hex"),
      name: "POST /checkout",
      kind: 2,
      startTimeUnixNano: start.toString(),
      endTimeUnixNano: (start + BigInt(20) * MS).toString(),
      status: { code: 2, message: "upstream failed" },
      attributes: [{ key: "http.response.status_code", value: { intValue: "500" } }],
    };
  });
  return {
    resourceSpans: [
      {
        resource: {
          attributes: [
            { key: "service.name", value: { stringValue: service } },
            { key: "service.instance.id", value: { stringValue: "e2e-1" } },
          ],
        },
        scopeSpans: [{ spans }],
      },
    ],
  };
}

test("a service's failing telemetry becomes an explained incident", async ({ page, request }) => {
  test.setTimeout(300_000);

  await page.goto("/register");
  await field(page, "Your name").fill("Erin");
  await field(page, "Work email").fill(`erin-${run}@example.com`);
  await field(page, "Password").fill(PASSWORD);
  await field(page, "Organization").fill(`Echo Retail ${run}`);
  await page.getByRole("button", { name: "Create account" }).click();
  await page.waitForURL(/\/orgs\/[0-9a-f-]+\/projects$/);

  await page.getByRole("button", { name: "New project" }).first().click();
  await field(page, "Name").fill("Storefront");
  await page.getByRole("button", { name: "Create project" }).click();
  await page.getByRole("link", { name: "Storefront" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Storefront" })).toBeVisible();
  await expect(page.getByText(/No telemetry yet\. Register a service/)).toBeVisible();

  // Service + key.
  await page.getByRole("link", { name: "Services" }).click();
  await page.getByRole("button", { name: "New service" }).first().click();
  await field(page, "Name").fill("checkout-api");
  await page.getByRole("button", { name: "Create service" }).click();
  const services = page.getByRole("region", { name: "Services" });
  await expect(services.getByText("checkout-api")).toBeVisible();
  await expect(services.getByText("No telemetry yet")).toBeVisible();
  await services.getByRole("button", { name: "Ingest keys" }).click();
  await page.getByRole("button", { name: "Create key" }).click();
  const key = (await page.getByTestId("new-api-key").textContent())?.trim() ?? "";
  expect(key).toMatch(/^sai_/);
  await page.getByRole("button", { name: "Done" }).click();
  await expect(page.getByTestId("new-api-key")).toHaveCount(0); // shown once
  await page.keyboard.press("Escape");

  // Fixture telemetry through the public ingest API.
  const sent = await request.post(`${INGEST_URL}/api/v1/otlp/v1/traces`, {
    headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
    data: failingSpans("checkout-api"),
  });
  expect(sent.status()).toBe(200);

  // The incident appears on its own (the list polls every 15 s).
  await page.getByRole("link", { name: "Incidents" }).first().click();
  await expect(page.getByRole("heading", { level: 1, name: "Incidents" })).toBeVisible();
  const incident = page.getByRole("link", { name: "Errors in checkout-api" });
  await expect(incident).toBeVisible({ timeout: 240_000 });
  await incident.click();

  await expect(page.getByRole("heading", { level: 1, name: "Errors in checkout-api" })).toBeVisible();
  await expect(page.getByText("Opened this incident")).toBeVisible();
  await expect(page.getByText(/Opened by Error rate on checkout-api/)).toBeVisible();
  // The engineer-level owner can act on it; the server records it in the timeline.
  await page.getByRole("button", { name: "Start investigating" }).click();
  await expect(page.getByText("Status open → investigating")).toBeVisible();

  // Overview and service map show the service with real data.
  await page.getByRole("link", { name: "Projects" }).first().click();
  await page.getByRole("link", { name: "Storefront" }).click();
  const latest = page.getByRole("region", { name: "Latest incidents" });
  await expect(latest.getByRole("link", { name: "Errors in checkout-api" })).toBeVisible();
  await expect(page.getByText(/telemetry \d+ (s|min) ago/).filter({ visible: true })).toBeVisible();
  await page.getByRole("link", { name: "Service map" }).click();
  await expect(page.getByLabel("Service map canvas").getByText("checkout-api")).toBeVisible();
});
