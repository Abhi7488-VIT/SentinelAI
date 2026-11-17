/**
 * Typed API client (openapi-fetch over the generated schema) with in-memory access token
 * and single-flight refresh. See docs/decisions/0001-auth.md.
 *
 * - The access token lives only in this module's memory (never localStorage).
 * - The refresh token is an httpOnly cookie the browser sends to /api/v1/auth/* only.
 * - On a 401 from any non-auth endpoint we refresh once (shared by concurrent callers, so a
 *   burst of 401s never presents the same refresh token twice — that would trigger the
 *   server's reuse detection and revoke the session) and retry the request once.
 */
import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(
  /\/$/,
  "",
);

type ErrorBody = components["schemas"]["ErrorResponse"];

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;
  readonly details: ErrorBody["error"]["details"];

  constructor(status: number, body: Partial<ErrorBody> | undefined) {
    const error = body?.error;
    super(error?.message ?? `Request failed (${status})`);
    this.name = "ApiError";
    this.status = status;
    this.code = error?.code ?? (status === 0 ? "network_error" : "http_error");
    this.requestId = error?.request_id ?? null;
    this.details = error?.details ?? null;
  }
}

// ---- access token store ------------------------------------------------------------------

let accessToken: string | null = null;
const listeners = new Set<() => void>();

export const tokenStore = {
  get: (): string | null => accessToken,
  set(token: string | null): void {
    if (token === accessToken) return;
    accessToken = token;
    for (const listener of listeners) listener();
  },
  subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => listeners.delete(listener);
  },
};

// ---- refresh (single flight) --------------------------------------------------------------

let refreshInFlight: Promise<string | null> | null = null;

export function refreshAccessToken(fetchImpl: typeof fetch = fetch): Promise<string | null> {
  refreshInFlight ??= (async () => {
    try {
      const res = await fetchImpl(`${API_URL}/api/v1/auth/refresh`, {
        method: "POST",
        credentials: "include",
      });
      if (!res.ok) {
        tokenStore.set(null);
        return null;
      }
      const body = (await res.json()) as components["schemas"]["TokenResponse"];
      tokenStore.set(body.access_token);
      return body.access_token;
    } catch {
      tokenStore.set(null);
      return null;
    } finally {
      refreshInFlight = null;
    }
  })();
  return refreshInFlight;
}

const AUTH_PATHS = ["/api/v1/auth/login", "/api/v1/auth/register", "/api/v1/auth/refresh", "/api/v1/auth/logout"];

function isAuthEndpoint(url: string): boolean {
  const path = new URL(url).pathname;
  return AUTH_PATHS.includes(path);
}

function withToken(request: Request, token: string | null): Request {
  if (token) request.headers.set("Authorization", `Bearer ${token}`);
  return request;
}

/** fetch used by the API client: adds the bearer token and retries once after a refresh. */
export function createAuthFetch(fetchImpl: typeof fetch = fetch) {
  return async function authFetch(input: Request): Promise<Response> {
    const retry = input.clone(); // bodies are single-use; keep a copy for the retry
    const response = await fetchImpl(withToken(input, tokenStore.get()));
    if (response.status !== 401 || isAuthEndpoint(input.url)) return response;
    const token = await refreshAccessToken(fetchImpl);
    if (!token) return response;
    return fetchImpl(withToken(retry, token));
  };
}

export const api = createClient<paths>({
  baseUrl: API_URL,
  credentials: "include",
  fetch: createAuthFetch((...args) => fetch(...args)),
});

/** Resolve an openapi-fetch result to its data, or throw an ApiError with the server's shape. */
export async function unwrap<T>(
  call: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  let result: { data?: T; error?: unknown; response: Response };
  try {
    result = await call;
  } catch {
    throw new ApiError(0, {
      error: { code: "network_error", message: "Can't reach the SentinelAI API.", request_id: null },
    });
  }
  if (!result.response.ok) {
    throw new ApiError(result.response.status, result.error as Partial<ErrorBody> | undefined);
  }
  return result.data as T;
}
