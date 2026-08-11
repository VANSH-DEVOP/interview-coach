/**
 * The BFF proxy: what reaches the API, and what reaches the browser.
 *
 * Two things are being pinned. First, that **no token ever comes back** — the
 * whole point of the change, and something a reader cannot verify from the
 * route handler alone because the stripping is generic rather than per-endpoint.
 *
 * Second, the distinction that moved here from the client and must not be
 * simplified away: **"the API refused the refresh token" and "the API could not
 * be asked" are different events.** Collapsing them once meant a momentary
 * outage cleared the session and sent people to a login page that could not
 * work either. Only a genuine rejection ends a session.
 */

import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DELETE, GET, POST } from "./[...path]/route";

const ORIGIN = "http://localhost:3000";

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function requestFor(
  path: string,
  { method = "GET", cookies = {}, body, origin = ORIGIN } = {} as {
    method?: string;
    cookies?: Record<string, string>;
    body?: unknown;
    origin?: string | null;
  },
): NextRequest {
  const headers = new Headers();
  if (origin) headers.set("origin", origin);
  const jar = Object.entries(cookies)
    .map(([k, v]) => `${k}=${v}`)
    .join("; ");
  if (jar) headers.set("cookie", jar);
  if (body !== undefined) headers.set("content-type", "application/json");

  return new NextRequest(`${ORIGIN}/api/bff/${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
}

const context = (path: string) => ({ params: Promise.resolve({ path: path.split("/") }) });

/** The Set-Cookie headers on a response, parsed enough to assert on. */
function cookiesOf(response: Response): Record<string, { value: string; raw: string }> {
  const out: Record<string, { value: string; raw: string }> = {};
  for (const raw of response.headers.getSetCookie()) {
    const [pair] = raw.split(";");
    const [name, ...rest] = pair.split("=");
    out[name.trim()] = { value: rest.join("="), raw };
  }
  return out;
}

const TOKENS = { access_token: "access-2", refresh_token: "refresh-2" };

// -- Nothing leaks to the browser ------------------------------------------------

describe("tokens never reach the browser", () => {
  it("moves a login's token pair into httpOnly cookies and strips it from the body", async () => {
    fetchMock.mockResolvedValueOnce(json({ ...TOKENS, token_type: "bearer" }));

    const response = await POST(
      requestFor("auth/login", { method: "POST", body: { email: "a@b.c", password: "x" } }),
      context("auth/login"),
    );

    const body = await response.json();
    expect(body.access_token).toBeUndefined();
    expect(body.refresh_token).toBeUndefined();
    // The rest of the payload survives.
    expect(body.token_type).toBe("bearer");

    const jar = cookiesOf(response);
    expect(jar.ip_access_token.value).toBe("access-2");
    // The entire feature: unreadable from JavaScript.
    expect(jar.ip_access_token.raw).toMatch(/HttpOnly/i);
    expect(jar.ip_refresh_token.raw).toMatch(/HttpOnly/i);
    // Withheld on cross-site POSTs, which is the CSRF defence now that the
    // credential travels automatically.
    expect(jar.ip_access_token.raw).toMatch(/SameSite=lax/i);
  });

  it("catches a token pair from any endpoint, not a list of known ones", async () => {
    // Password reset issues a pair too, and so will whatever is added next.
    // Matching on the shape rather than the path is what stops a new endpoint
    // leaking tokens by being forgotten here.
    fetchMock.mockResolvedValueOnce(json(TOKENS));

    const response = await POST(
      requestFor("auth/reset-password", { method: "POST", body: { token: "t" } }),
      context("auth/reset-password"),
    );

    expect(await response.json()).toEqual({});
    expect(cookiesOf(response).ip_access_token.value).toBe("access-2");
  });
});

// -- The credential goes the other way -------------------------------------------

describe("attaching the session", () => {
  it("sends the cookie's access token as a bearer token", async () => {
    fetchMock.mockResolvedValueOnce(json({ id: 1 }));

    await GET(
      requestFor("users/me", { cookies: { ip_access_token: "access-1" } }),
      context("users/me"),
    );

    const headers = fetchMock.mock.calls[0][1].headers as Headers;
    expect(headers.get("authorization")).toBe("Bearer access-1");
    // The browser's cookie header must not be forwarded: the API authenticates
    // on the bearer token and has no use for it.
    expect(headers.get("cookie")).toBeNull();
  });

  it("supplies the refresh token on logout, which the browser no longer holds", async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));

    const response = await POST(
      requestFor("auth/logout", {
        method: "POST",
        body: { everywhere: false },
        cookies: { ip_refresh_token: "refresh-1" },
      }),
      context("auth/logout"),
    );

    // Forwarded as bytes, so a multipart upload keeps its boundary intact.
    const sent = JSON.parse(
      new TextDecoder().decode(fetchMock.mock.calls[0][1].body as ArrayBuffer),
    );
    expect(sent.refresh_token).toBe("refresh-1");
    // And the session ends here regardless of what the API said.
    expect(cookiesOf(response).ip_access_token.value).toBe("");
  });
});

// -- Refresh: the three outcomes -------------------------------------------------

describe("refresh on 401", () => {
  it("refreshes, replays the request, and rotates the cookies", async () => {
    fetchMock
      .mockResolvedValueOnce(json({ error: { code: "unauthorized" } }, 401))
      .mockResolvedValueOnce(json(TOKENS))
      .mockResolvedValueOnce(json({ id: 1 }));

    const response = await GET(
      requestFor("users/me", {
        cookies: { ip_access_token: "old", ip_refresh_token: "refresh-1" },
      }),
      context("users/me"),
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ id: 1 });
    expect(cookiesOf(response).ip_access_token.value).toBe("access-2");
    // The replay carried the new token, not the expired one.
    const replay = fetchMock.mock.calls[2][1].headers as Headers;
    expect(replay.get("authorization")).toBe("Bearer access-2");
  });

  it("ends the session when the API actually rejects the refresh token", async () => {
    fetchMock
      .mockResolvedValueOnce(json({ error: { code: "unauthorized" } }, 401))
      .mockResolvedValueOnce(json({ error: { code: "token_revoked" } }, 401));

    const response = await GET(
      requestFor("users/me", {
        cookies: { ip_access_token: "old", ip_refresh_token: "dead" },
      }),
      context("users/me"),
    );

    expect(response.status).toBe(401);
    const jar = cookiesOf(response);
    expect(jar.ip_access_token.value).toBe("");
    expect(jar.ip_refresh_token.value).toBe("");
  });

  it("keeps the session when the refresh endpoint cannot be reached", async () => {
    fetchMock
      .mockResolvedValueOnce(json({ error: { code: "unauthorized" } }, 401))
      .mockRejectedValueOnce(new TypeError("fetch failed"));

    const response = await GET(
      requestFor("users/me", {
        cookies: { ip_access_token: "old", ip_refresh_token: "refresh-1" },
      }),
      context("users/me"),
    );

    // Nobody asked whether the refresh token was still good, so it is not
    // thrown away. Clearing here is what made an outage look like a logout.
    expect(cookiesOf(response)).toEqual({});
    // And the caller is told the truth: transient, not "your session ended".
    expect(response.status).toBe(503);
  });

  it("keeps the session when the refresh endpoint returns a 500", async () => {
    fetchMock
      .mockResolvedValueOnce(json({ error: { code: "unauthorized" } }, 401))
      .mockResolvedValueOnce(json({ error: { code: "internal_error" } }, 500));

    const response = await GET(
      requestFor("users/me", {
        cookies: { ip_access_token: "old", ip_refresh_token: "refresh-1" },
      }),
      context("users/me"),
    );

    expect(cookiesOf(response)).toEqual({});
    expect(response.status).toBe(503);
  });

  it("does not try to refresh without a refresh cookie", async () => {
    fetchMock.mockResolvedValueOnce(json({ error: { code: "unauthorized" } }, 401));

    const response = await GET(requestFor("users/me"), context("users/me"));

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(response.status).toBe(401);
  });

  it("retries at most once, so a persistently-401 endpoint cannot loop", async () => {
    fetchMock
      .mockResolvedValueOnce(json({ error: { code: "unauthorized" } }, 401))
      .mockResolvedValueOnce(json(TOKENS))
      .mockResolvedValueOnce(json({ error: { code: "unauthorized" } }, 401));

    const response = await GET(
      requestFor("users/me", {
        cookies: { ip_access_token: "old", ip_refresh_token: "refresh-1" },
      }),
      context("users/me"),
    );

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(response.status).toBe(401);
  });
});

// -- Everything else it must not break --------------------------------------------

describe("pass-through", () => {
  it("refuses a cross-origin request", async () => {
    const response = await POST(
      requestFor("users/me", { method: "POST", origin: "https://evil.test" }),
      context("users/me"),
    );

    expect(response.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("forwards a non-JSON body as bytes rather than parsing it", async () => {
    // A PDF export would be corrupted by a round trip through JSON.parse.
    const pdf = new Uint8Array([0x25, 0x50, 0x44, 0x46]);
    fetchMock.mockResolvedValueOnce(
      new Response(pdf, { status: 200, headers: { "content-type": "application/pdf" } }),
    );

    const response = await GET(
      requestFor("reports/1/export", { cookies: { ip_access_token: "a" } }),
      context("reports/1/export"),
    );

    expect(response.headers.get("content-type")).toBe("application/pdf");
    expect(new Uint8Array(await response.arrayBuffer())).toEqual(pdf);
  });

  it("passes a 204 through without a body", async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));

    const response = await DELETE(
      requestFor("interviews/1", { method: "DELETE", cookies: { ip_access_token: "a" } }),
      context("interviews/1"),
    );

    expect(response.status).toBe(204);
  });

  it("answers the error envelope when the API is unreachable", async () => {
    // Shaped like the backend's own so ApiError parses it without a special
    // case, and 5xx so `isTransient` reads it as an outage rather than a logout.
    fetchMock.mockRejectedValueOnce(new TypeError("fetch failed"));

    const response = await GET(
      requestFor("users/me", { cookies: { ip_access_token: "a" } }),
      context("users/me"),
    );

    expect(response.status).toBe(503);
    expect((await response.json()).error.code).toBe("upstream_unavailable");
  });

  it("keeps the query string", async () => {
    fetchMock.mockResolvedValueOnce(json({ items: [] }));
    const request = new NextRequest(`${ORIGIN}/api/bff/interviews?page=2&size=10`, {
      headers: new Headers({ origin: ORIGIN }),
    });

    await GET(request, context("interviews"));

    expect(fetchMock.mock.calls[0][0]).toMatch(/\/interviews\?page=2&size=10$/);
  });
});
