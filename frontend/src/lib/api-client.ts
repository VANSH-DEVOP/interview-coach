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

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details?: unknown
  ) {
    super(message);
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

  const response = await fetch(`${API_URL}${path}`, { ...options, headers });

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

async function tryRefresh(): Promise<boolean> {
  const refreshToken = readCookie(REFRESH_TOKEN_KEY);
  if (!refreshToken) return false;
  try {
    const tokens = await rawRequest<TokenPair>(
      "/auth/refresh",
      { method: "POST", body: JSON.stringify({ refresh_token: refreshToken }) },
      null
    );
    setTokens(tokens);
    return true;
  } catch {
    clearTokens();
    return false;
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  try {
    return await rawRequest<T>(path, options);
  } catch (error) {
    // One transparent retry after refreshing an expired access token.
    if (error instanceof ApiError && error.status === 401 && (await tryRefresh())) {
      return rawRequest<T>(path, options);
    }
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
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};
