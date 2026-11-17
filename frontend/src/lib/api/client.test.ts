import { afterEach, describe, expect, it, vi } from "vitest";

import { API_URL, createAuthFetch, refreshAccessToken, tokenStore } from "@/lib/api/client";

afterEach(() => tokenStore.set(null));

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function urlOf(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.href;
  return input.url;
}

describe("auth fetch", () => {
  it("adds the bearer token", async () => {
    tokenStore.set("t1");
    const fetchImpl = vi.fn<typeof fetch>(async () => json(200, {}));
    await createAuthFetch(fetchImpl)(new Request(`${API_URL}/api/v1/orgs`));
    const sent = fetchImpl.mock.calls[0][0] as Request;
    expect(sent.headers.get("Authorization")).toBe("Bearer t1");
  });

  it("refreshes once for concurrent 401s and retries each request with the new token", async () => {
    tokenStore.set("expired");
    let refreshCalls = 0;
    const fetchImpl = vi.fn<typeof fetch>(async (input) => {
      if (urlOf(input).endsWith("/api/v1/auth/refresh")) {
        refreshCalls += 1;
        await new Promise((resolve) => setTimeout(resolve, 10));
        return json(200, { access_token: "fresh", token_type: "bearer", expires_in: 900 });
      }
      const auth = (input as Request).headers.get("Authorization");
      return auth === "Bearer fresh"
        ? json(200, { ok: true })
        : json(401, { error: { code: "token_expired" } });
    });
    const authFetch = createAuthFetch(fetchImpl);
    const results = await Promise.all([
      authFetch(new Request(`${API_URL}/api/v1/orgs`)),
      authFetch(new Request(`${API_URL}/api/v1/auth/me`)),
      authFetch(
        new Request(`${API_URL}/api/v1/orgs/x/projects`, { method: "POST", body: '{"name":"a"}' }),
      ),
    ]);
    expect(refreshCalls).toBe(1);
    expect(results.map((r) => r.status)).toEqual([200, 200, 200]);
    expect(tokenStore.get()).toBe("fresh");
  });

  it("does not try to refresh after a failed login", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      json(401, { error: { code: "invalid_credentials" } }),
    );
    const res = await createAuthFetch(fetchImpl)(
      new Request(`${API_URL}/api/v1/auth/login`, { method: "POST", body: "{}" }),
    );
    expect(res.status).toBe(401);
    expect(fetchImpl).toHaveBeenCalledOnce();
  });

  it("signs out when the refresh is rejected", async () => {
    tokenStore.set("expired");
    const fetchImpl = vi.fn<typeof fetch>(async () =>
      json(401, { error: { code: "refresh_token_invalid" } }),
    );
    expect(await refreshAccessToken(fetchImpl)).toBeNull();
    expect(tokenStore.get()).toBeNull();
  });
});
