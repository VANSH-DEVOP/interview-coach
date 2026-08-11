/**
 * Server-side pieces of the backend-for-frontend: where the tokens live.
 *
 * Nothing here runs in the browser. The whole point of the BFF is that the
 * access and refresh tokens exist only in httpOnly cookies and in Next's server
 * process — an injected script can no longer read them, which is what
 * `document.cookie` storage could never prevent.
 */

import type { NextRequest } from "next/server";

export const ACCESS_COOKIE = "ip_access_token";
export const REFRESH_COOKIE = "ip_refresh_token";

/** Kept in step with the backend's ACCESS_TOKEN_EXPIRE_MINUTES / REFRESH_TOKEN_EXPIRE_DAYS. */
const ACCESS_MAX_AGE = 60 * 30;
const REFRESH_MAX_AGE = 60 * 60 * 24 * 7;

/**
 * Where this server talks to the API.
 *
 * Deliberately *not* `NEXT_PUBLIC_API_URL`. That one is baked into the browser
 * bundle and has to be an address the browser can reach; this is a call from
 * one container to another, where the browser's `localhost:8000` means the
 * frontend container itself. In compose the right value is
 * `http://backend:8000/api/v1`.
 */
export function apiBaseUrl(): string {
  return (
    process.env.API_INTERNAL_URL ??
    process.env.NEXT_PUBLIC_API_URL ??
    "http://localhost:8000/api/v1"
  );
}

export interface CookieSpec {
  name: string;
  value: string;
  httpOnly: boolean;
  secure: boolean;
  sameSite: "lax";
  path: string;
  maxAge: number;
}

function cookie(name: string, value: string, maxAge: number): CookieSpec {
  return {
    name,
    value,
    // The entire feature. Unreadable from JavaScript, so an XSS that would
    // previously have exfiltrated the session now cannot see it.
    httpOnly: true,
    // Off over plain HTTP or the cookie is never stored in local development.
    secure: process.env.NODE_ENV === "production",
    // Lax, not None: the browser withholds these on cross-site POSTs, which is
    // the primary CSRF defence now that credentials travel automatically
    // instead of in an Authorization header the attacker cannot forge.
    sameSite: "lax",
    path: "/",
    maxAge,
  };
}

export function sessionCookies(tokens: {
  access_token: string;
  refresh_token: string;
}): CookieSpec[] {
  return [
    cookie(ACCESS_COOKIE, tokens.access_token, ACCESS_MAX_AGE),
    cookie(REFRESH_COOKIE, tokens.refresh_token, REFRESH_MAX_AGE),
  ];
}

/** maxAge 0 deletes. Same attributes, or the browser keeps the original. */
export function clearedCookies(): CookieSpec[] {
  return [cookie(ACCESS_COOKIE, "", 0), cookie(REFRESH_COOKIE, "", 0)];
}

/** A response body that carries a token pair, whichever endpoint produced it. */
export function tokensIn(body: unknown): { access_token: string; refresh_token: string } | null {
  if (typeof body !== "object" || body === null) return null;
  const candidate = body as Record<string, unknown>;
  if (
    typeof candidate.access_token === "string" &&
    typeof candidate.refresh_token === "string"
  ) {
    return {
      access_token: candidate.access_token,
      refresh_token: candidate.refresh_token,
    };
  }
  return null;
}

/**
 * Reject a cross-site request before it can spend a cookie.
 *
 * `SameSite=Lax` already withholds the cookies on a cross-site POST, so this is
 * the second layer rather than the only one — and the cheap kind, since a
 * browser will not let a page forge `Origin`.
 *
 * Same-origin requests from older browsers may send no Origin at all on GET,
 * so an absent header is allowed and only a *mismatched* one is refused.
 */
export function isSameOrigin(request: NextRequest): boolean {
  const origin = request.headers.get("origin");
  if (!origin) return true;
  try {
    return new URL(origin).origin === new URL(request.url).origin;
  } catch {
    return false;
  }
}
