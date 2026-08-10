/**
 * Typed API client with JWT auth and automatic access-token refresh.
 *
 * Token storage (MVP): cookies readable by the Next.js middleware for route
 * protection. Harden later by moving refresh tokens to httpOnly cookies set
 * by a BFF route.
 */

import type { ApiErrorBody, TokenPair } from "@/types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000/api/v1";

const ACCESS_TOKEN_KEY = "ip_access_token";
const REFRESH_TOKEN_KEY = "ip_refresh_token";

/** What kind of failure this was, which is what callers actually branch on. */
export type ErrorKind =
  /** The server said no to these credentials: 401 or 403. */
  | "auth"
  /** The request was wrong: 4xx other than 401/403. */
  | "client"
  /** The server broke: 5xx. */
  | "server"
  /** No response at all -- offline, DNS, CORS, the API is not running. */
  | "network";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details?: unknown
  ) {
    super(message);
  }

  get kind(): ErrorKind {
    if (this.status === 0) return "network";
    if (this.status === 401 || this.status === 403) return "auth";
    if (this.status >= 500) return "server";
    return "client";
  }

  /**
   * True when retrying later might work, and the session is *not* implicated.
   *
   * The distinction the UI needs: an auth failure means sign in again, while
   * everything here means the credentials are fine and the server is not.
   * Treating the second as the first is what makes an outage look like a
   * logout.
   */
  get isTransient(): boolean {
    return this.kind === "network" || this.kind === "server";
  }
}

/**
 * A request that never reached the server.
 *
 * Subclasses ApiError rather than standing alone so that the `err instanceof
 * ApiError ? err.message : "..."` pattern every page already uses shows the
 * message below instead of falling through to a generic one. Status 0 is the
 * usual convention for "no response"; `kind` reads it back as "network".
 */
export class NetworkError extends ApiError {
  constructor(public cause?: unknown) {
    super(0, "network_error", "Unable to reach the server. Check your connection and try again.");
  }
}

// -- Token management ---------------------------------------------------------

function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}

function writeCookie(name: string, value: string, maxAgeSeconds: number): void {
  document.cookie = `${name}=${encodeURIComponent(value)}; path=/; max-age=${maxAgeSeconds}; samesite=lax`;
}

function clearCookie(name: string): void {
  document.cookie = `${name}=; path=/; max-age=0`;
}

export function setTokens(tokens: TokenPair): void {
  writeCookie(ACCESS_TOKEN_KEY, tokens.access_token, 60 * 30);
  writeCookie(REFRESH_TOKEN_KEY, tokens.refresh_token, 60 * 60 * 24 * 7);
}

export function clearTokens(): void {
  clearCookie(ACCESS_TOKEN_KEY);
  clearCookie(REFRESH_TOKEN_KEY);
}

export function getAccessToken(): string | null {
  return readCookie(ACCESS_TOKEN_KEY);
}

export function getRefreshToken(): string | null {
  return readCookie(REFRESH_TOKEN_KEY);
}

// -- Core request -------------------------------------------------------------

async function rawRequest<T>(
  path: string,
  options: RequestInit = {},
  token: string | null = getAccessToken()
): Promise<T> {
  const headers = new Headers(options.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (options.body && !(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }

  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, { ...options, headers });
  } catch (error) {
    // fetch rejects only when no response was received: offline, DNS failure,
    // connection refused, CORS. An HTTP error status resolves normally and is
    // handled below. Without this the caller gets a bare TypeError
    // ("Failed to fetch"), which is neither an ApiError nor showable.
    throw new NetworkError(error);
  }

  if (response.status === 204) return undefined as T;

  if (!response.ok) {
    let body: ApiErrorBody | null = null;
    try {
      body = (await response.json()) as ApiErrorBody;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(
      response.status,
      body?.error.code ?? "unknown_error",
      body?.error.message ?? "Request failed.",
      body?.error.details
    );
  }

  return (await response.json()) as T;
}

type RefreshOutcome =
  /** New tokens are stored; replay the original request. */
  | { status: "refreshed" }
  /** The server refused the refresh token. The session is over. */
  | { status: "rejected" }
  /** Could not find out -- the server was unreachable or broken. */
  | { status: "unavailable"; error: ApiError };

/**
 * Exchange the refresh token for a new pair.
 *
 * The three outcomes exist because this used to have two, and collapsing
 * "refused" into "could not ask" is a bug with teeth: a refresh attempted
 * while offline cleared the tokens, so a momentary network drop *destroyed a
 * live session* and dumped the user on the login page. Tokens are now cleared
 * only when the server itself rejects them.
 */
async function tryRefresh(): Promise<RefreshOutcome> {
  const refreshToken = readCookie(REFRESH_TOKEN_KEY);
  if (!refreshToken) return { status: "rejected" };
  try {
    const tokens = await rawRequest<TokenPair>(
      "/auth/refresh",
      { method: "POST", body: JSON.stringify({ refresh_token: refreshToken }) },
      null
    );
    setTokens(tokens);
    return { status: "refreshed" };
  } catch (error) {
    if (error instanceof ApiError && error.isTransient) {
      return { status: "unavailable", error };
    }
    clearTokens();
    return { status: "rejected" };
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  try {
    return await rawRequest<T>(path, options);
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 401) throw error;

    // One transparent retry after refreshing an expired access token.
    const outcome = await tryRefresh();
    if (outcome.status === "refreshed") return rawRequest<T>(path, options);
    // The 401 came back because we could not renew the token, not because the
    // session is finished. Reporting it as "unauthorized" would send the user
    // to a login page that cannot work either; report what actually happened.
    if (outcome.status === "unavailable") throw outcome.error;
    throw error;
  }
}

// -- Public surface -------------------------------------------------------------

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: body !== undefined ? JSON.stringify(body) : undefined }),
  postForm: <T>(path: string, form: FormData) =>
    request<T>(path, { method: "POST", body: form }),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body) }),
  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
  // Body is optional and rarely used, but account deletion sends the password
  // this way rather than in a query string, where it would land in access logs
  // and browser history.
  delete: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: "DELETE",
      body: body !== undefined ? JSON.stringify(body) : undefined,
    }),
};
