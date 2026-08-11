/**
 * The backend-for-frontend proxy. Every API call the browser makes goes here.
 *
 * **Why all of them and not just the auth ones.** The point is that the browser
 * never holds a token. If ordinary requests went straight to the API, they
 * would need an `Authorization` header, which means JavaScript would need the
 * token, which is the thing being removed. So this proxies everything and
 * attaches the credential server-side.
 *
 * What the browser sends is a same-origin request with an httpOnly cookie it
 * cannot read. What leaves this process is the same request with a Bearer
 * token. Nothing else about the API contract changes — the paths, bodies,
 * status codes and error envelope are all passed through untouched, which is
 * why the pages and `api-client.ts` above did not have to learn anything.
 *
 * ## Token refresh moved here, and its three outcomes came with it
 *
 * The client used to refresh its own tokens, and the distinction it drew is the
 * one thing about this file that must not be simplified: **"the server refused
 * the token" and "the server could not be asked" are different events.** A
 * refresh attempted while the API is down used to clear the session, so a
 * momentary outage logged people out and sent them to a login page that could
 * not work either. Only a genuine rejection ends a session; anything else is
 * reported as the transport failure it is, with the cookies left alone.
 */

import { NextRequest, NextResponse } from "next/server";

import {
  ACCESS_COOKIE,
  REFRESH_COOKIE,
  apiBaseUrl,
  clearedCookies,
  isSameOrigin,
  sessionCookies,
  tokensIn,
} from "@/lib/bff";

/** Hop-by-hop and length headers that must not be forwarded verbatim. */
const SKIPPED_REQUEST_HEADERS = new Set([
  "host",
  "connection",
  "content-length",
  "cookie",
  "accept-encoding",
]);
const SKIPPED_RESPONSE_HEADERS = new Set([
  "content-length",
  "content-encoding",
  "transfer-encoding",
  "connection",
  "set-cookie",
]);

function jsonError(status: number, code: string, message: string): NextResponse {
  // The backend's envelope, reproduced exactly, so a failure originating in
  // this proxy is indistinguishable in shape from one the API produced and
  // `ApiError` parses it without a special case.
  return NextResponse.json({ error: { code, message, details: null } }, { status });
}

async function callApi(
  path: string,
  request: NextRequest,
  body: ArrayBuffer | null,
  accessToken: string | null,
): Promise<Response> {
  const headers = new Headers();
  request.headers.forEach((value, key) => {
    if (!SKIPPED_REQUEST_HEADERS.has(key.toLowerCase())) headers.set(key, value);
  });
  if (accessToken) headers.set("authorization", `Bearer ${accessToken}`);

  const url = new URL(request.url);
  return fetch(`${apiBaseUrl()}/${path}${url.search}`, {
    method: request.method,
    headers,
    body: body && body.byteLength > 0 ? body : undefined,
    // Never follow a redirect on the API's behalf: the browser should see it.
    redirect: "manual",
    cache: "no-store",
  });
}

type Refresh =
  | { status: "refreshed"; access: string; refresh: string }
  /** The API refused the refresh token. The session is genuinely over. */
  | { status: "rejected" }
  /** Could not find out. The session is untouched. */
  | { status: "unavailable" };

async function refreshTokens(refreshToken: string): Promise<Refresh> {
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl()}/auth/refresh`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
      cache: "no-store",
    });
  } catch {
    // Unreachable. Emphatically not a rejection.
    return { status: "unavailable" };
  }

  if (response.status >= 500) return { status: "unavailable" };
  if (!response.ok) return { status: "rejected" };

  const body = await response.json().catch(() => null);
  const tokens = tokensIn(body);
  return tokens
    ? { status: "refreshed", access: tokens.access_token, refresh: tokens.refresh_token }
    : { status: "rejected" };
}

async function proxy(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
): Promise<NextResponse> {
  if (!isSameOrigin(request)) {
    return jsonError(403, "forbidden", "Cross-origin requests are not accepted here.");
  }

  const { path } = await context.params;
  const target = path.join("/");

  // Read once: the stream cannot be replayed for the retry below, and an
  // upload is capped at 5 MiB by the API anyway.
  const body =
    request.method === "GET" || request.method === "HEAD"
      ? null
      : await request.arrayBuffer();

  const cookieStore = request.cookies;
  const accessToken = cookieStore.get(ACCESS_COOKIE)?.value ?? null;
  const refreshToken = cookieStore.get(REFRESH_COOKIE)?.value ?? null;

  // Logout surrenders the refresh token, and the browser no longer has one to
  // surrender. Supplying it here is the whole reason this endpoint needs a
  // special case: the alternative is handing the token back to JavaScript for
  // the length of one request, which is exactly what this proxy exists to stop.
  const outgoing =
    target === "auth/logout" && refreshToken
      ? new TextEncoder().encode(JSON.stringify({ refresh_token: refreshToken }))
          .buffer as ArrayBuffer
      : body;

  let response: Response;
  try {
    response = await callApi(target, request, outgoing, accessToken);
  } catch {
    return jsonError(
      503,
      "upstream_unavailable",
      "Unable to reach the server. Check your connection and try again.",
    );
  }

  let rotated: { access: string; refresh: string } | null = null;
  let endSession = false;

  if (response.status === 401 && refreshToken) {
    const outcome = await refreshTokens(refreshToken);
    if (outcome.status === "refreshed") {
      rotated = { access: outcome.access, refresh: outcome.refresh };
      try {
        response = await callApi(target, request, outgoing, outcome.access);
      } catch {
        return jsonError(
          503,
          "upstream_unavailable",
          "Unable to reach the server. Check your connection and try again.",
        );
      }
    } else if (outcome.status === "unavailable") {
      // The 401 stands, but the session does not end over it: we never found
      // out whether the refresh token was still good.
      return jsonError(
        503,
        "upstream_unavailable",
        "Unable to reach the server. Check your connection and try again.",
      );
    } else {
      endSession = true;
    }
  }

  return await forward(response, { rotated, endSession, path: target });
}

async function forward(
  upstream: Response,
  options: {
    rotated: { access: string; refresh: string } | null;
    endSession: boolean;
    path: string;
  },
): Promise<NextResponse> {
  const headers = new Headers();
  upstream.headers.forEach((value, key) => {
    if (!SKIPPED_RESPONSE_HEADERS.has(key.toLowerCase())) headers.set(key, value);
  });

  const contentType = upstream.headers.get("content-type") ?? "";
  const isJson = contentType.includes("application/json");

  let out: NextResponse;
  if (upstream.status === 204 || !isJson) {
    // Anything that is not JSON — a PDF export, for instance — is passed
    // through as bytes. Parsing it to look for tokens would corrupt it.
    const buffer = upstream.status === 204 ? null : await upstream.arrayBuffer();
    out = new NextResponse(buffer, { status: upstream.status, headers });
  } else {
    const parsed = await upstream.json().catch(() => null);
    const issued = tokensIn(parsed);
    if (issued) {
      // Login, refresh and password reset all answer with a token pair. They
      // are caught generically rather than by path, so an endpoint that starts
      // issuing tokens tomorrow cannot leak them to the browser by being
      // forgotten here.
      options.rotated = { access: issued.access_token, refresh: issued.refresh_token };
      const { access_token: _a, refresh_token: _r, ...rest } = parsed as Record<
        string,
        unknown
      >;
      out = NextResponse.json(rest, { status: upstream.status, headers });
    } else {
      out = NextResponse.json(parsed, { status: upstream.status, headers });
    }
  }

  if (options.rotated) {
    for (const spec of sessionCookies({
      access_token: options.rotated.access,
      refresh_token: options.rotated.refresh,
    })) {
      out.cookies.set(spec);
    }
  }

  // Logging out surrenders the refresh token, so the cookies go with it —
  // whatever the API answered, since an already-invalid token still means the
  // user asked to be signed out.
  if (options.endSession || options.path === "auth/logout") {
    for (const spec of clearedCookies()) out.cookies.set(spec);
  }

  return out;
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;

// Tokens and user data must never be cached at the edge or in the browser.
export const dynamic = "force-dynamic";
