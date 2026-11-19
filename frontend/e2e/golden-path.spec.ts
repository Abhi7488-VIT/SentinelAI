import { expect, type Page, test } from "@playwright/test";

/*
 * Phase 1 acceptance flow, in a real browser against the real API:
 * register -> create project -> see it listed -> logout -> login -> see it again
 * -> a second user in another org cannot see it (in their list, or by direct URL).
 */

const PASSWORD = "correct horse battery staple";

/*
 * With Cache Components, Next keeps the previous route mounted but hidden (for instant back
 * navigation), so a label can match a hidden field from the page we just left. Always target
 * the visible field with an exact label.
 */
function field(page: Page, label: string | RegExp) {
  return page.getByLabel(label, { exact: true }).filter({ visible: true });
}
const run = Date.now().toString(36);

async function register(page: Page, name: string, email: string, org: string): Promise<string> {
  await page.goto("/register");
  await field(page, "Your name").fill(name);
  await field(page, "Work email").fill(email);
  await field(page, "Password").fill(PASSWORD);
  await field(page, "Organization").fill(org);
  await page.getByRole("button", { name: "Create account" }).click();
  await page.waitForURL(/\/orgs\/[0-9a-f-]+\/projects$/);
  await expect(page.getByRole("heading", { level: 1, name: "Projects" })).toBeVisible();
  const orgId = /\/orgs\/([0-9a-f-]+)\/projects/.exec(page.url())?.[1];
  expect(orgId).toBeTruthy();
  return orgId as string;
}

async function signOut(page: Page): Promise<void> {
  await page.getByRole("button", { name: "Account" }).click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await page.waitForURL(/\/login$/);
}

test("register, create a project, sign out and in, and stay isolated from another org", async ({
  page,
}) => {
  const alice = `alice-${run}@example.com`;
  const orgA = await register(page, "Alice", alice, `Alpha Payments ${run}`);

  // Empty state first: the frame stays and offers the create action.
  await expect(page.getByText("No projects yet.")).toBeVisible();

  // Create a project through the dialog.
  await page.getByRole("button", { name: "New project" }).first().click();
  await field(page, "Name").fill("Checkout API");
  await field(page, /^Description/).fill("Cart, payment intents and order confirmation");
  await page.getByRole("button", { name: "Create project" }).click();
  const projects = page.getByRole("region", { name: "Projects" });
  await expect(projects.getByText("Checkout API")).toBeVisible();
  await expect(projects.getByText("checkout-api")).toBeVisible();
  await expect(page.getByText("1 project", { exact: true })).toBeVisible();

  // Survives a full reload: the session is restored from the refresh cookie.
  await page.reload();
  await expect(projects.getByText("Checkout API")).toBeVisible();

  // Sign out, then back in.
  await signOut(page);
  await field(page, "Email").fill(alice);
  await field(page, "Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(new RegExp(`/orgs/${orgA}/projects$`));
  await expect(projects.getByText("Checkout API")).toBeVisible();
  await signOut(page);

  // A second user in their own org sees none of it.
  const orgB = await register(page, "Bob", `bob-${run}@example.com`, `Beta Search ${run}`);
  expect(orgB).not.toBe(orgA);
  await expect(page.getByText("No projects yet.")).toBeVisible();
  await expect(page.getByText("Checkout API")).toHaveCount(0);

  // ...not even by typing the other org's URL.
  await page.goto(`/orgs/${orgA}/projects`);
  await expect(page.getByRole("heading", { level: 1, name: "Organization not found" })).toBeVisible();
  await expect(page.getByText("Checkout API")).toHaveCount(0);

  // And the API itself refuses (404, not 403: existence is not revealed).
  const status = await page.evaluate(async (orgId) => {
    const refresh = await fetch("http://localhost:8000/api/v1/auth/refresh", {
      method: "POST",
      credentials: "include",
    });
    const { access_token } = (await refresh.json()) as { access_token: string };
    const res = await fetch(`http://localhost:8000/api/v1/orgs/${orgId}/projects`, {
      headers: { Authorization: `Bearer ${access_token}` },
    });
    return res.status;
  }, orgA);
  expect(status).toBe(404);
});
