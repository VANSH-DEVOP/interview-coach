/**
 * Typed API client. Same-origin, and it holds no credentials.
 *
 * Requests go to this app's own `/api/bff/*` proxy rather than to the API
 * directly, and the session travels as an httpOnly cookie the browser attaches
 * by itself. **There is deliberately no token handling in this file.** Tokens
 * used to live in `document.cookie`, which meant any injected script could read
 * the session; they now exist only in the Next server process, and this module
 * cannot see them even if it wanted to.
 *
 * Refresh moved to the proxy along with them, which is why the retry logic that
 * used to be here is gone rather than merely relocated. Its three outcomes are
 * preserved there — see the note in the route handler about why "refused" and
 * "could not be asked" must stay different events.
 */

import type { ApiErrorBody } from "@/types";

/**
 * Same-origin and relative on purpose. An absolute URL would reintroduce the
 * cross-origin request this design removes, and with it the CORS configuration
 * and the need for the browser to hold a credential of its own.
 */
const API_URL = "/api/bff";

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

// -- Core request -------------------------------------------------------------

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
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
