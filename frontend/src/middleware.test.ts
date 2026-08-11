/**
 * The middleware does route protection and Content-Security-Policy, and both
 * have failed silently here before.
 *
 * The route guard was dead for the life of the project: `middleware.ts` lived
 * at the repository root while the app lives under `src/`, so Next never loaded
 * it and `/dashboard` answered 200 to anyone. A unit test cannot catch that on
 * its own — importing the function and calling it works perfectly well on a
 * file Next is ignoring — so what these tests rest on is the file being *next
 * to* `src/middleware.ts`. Move one and move both.
 *
 * The CSP failed the other way. A nonce that does not match the scripts on the
 * page blocks all of them, and the response still has status 200 and looks
 * perfect to anything that is not a browser.
 */

import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";

import { middleware } from "./middleware";

function request(path: string, cookie?: string): NextRequest {
  const headers = new Headers();
  if (cookie) headers.set("cookie", cookie);
  return new NextRequest(new URL(`https://app.test${path}`), { headers });
}

function policyOf(path: string, cookie?: string): string {
  return middleware(request(path, cookie)).headers.get("content-security-policy") ?? "";
}

function directive(policy: string, name: string): string {
  const found = policy
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${name} `) || part === name);
  return found ?? "";
}

describe("route protection", () => {
  it("sends an anonymous visitor from an app page to login, remembering where", () => {
    const response = middleware(request("/dashboard"));

    expect(response.status).toBe(307);
    const location = new URL(response.headers.get("location")!);
    expect(location.pathname).toBe("/login");
    expect(location.searchParams.get("next")).toBe("/dashboard");
  });

  it("sends a signed-in visitor away from the auth pages", () => {
    const response = middleware(request("/login", "ip_refresh_token=abc"));

    expect(response.status).toBe(307);
    expect(new URL(response.headers.get("location")!).pathname).toBe("/dashboard");
  });

  it("leaves everything else alone", () => {
    expect(middleware(request("/")).status).toBe(200);
  });
});

describe("content security policy", () => {
  it("is set on every response, including the redirects", () => {
    // A redirect carries a body in some browsers, and one unprotected response
    // is all a gap needs.
    expect(policyOf("/dashboard")).toContain("default-src 'self'");
    expect(policyOf("/login", "ip_refresh_token=abc")).toContain("default-src 'self'");
    expect(policyOf("/")).toContain("default-src 'self'");
  });

  it("carries a fresh nonce per response", () => {
    const nonces = new Set(
      Array.from({ length: 5 }, () => policyOf("/").match(/nonce-([^']+)/)?.[1])
    );

    expect(nonces.size).toBe(5);
    expect([...nonces].every((nonce) => nonce && nonce.length >= 16)).toBe(true);
  });

  it("puts the nonce on the request too, which is how Next finds it", () => {
    // Without this the header is decoration: Next reads the nonce off the
    // request to stamp it on the inline scripts it emits, and a policy whose
    // nonce matches nothing blocks every script on the page.
    const response = middleware(request("/"));
    const policy = response.headers.get("content-security-policy")!;
    const nonce = policy.match(/nonce-([^']+)/)![1];

    expect(response.headers.get("x-middleware-override-headers")).toContain("x-nonce");
    expect(nonce.length).toBeGreaterThan(16);
  });

  it("never allows inline script", () => {
    // Still the whole point after the BFF, though for a narrower reason: an
    // injected script can no longer read the session, but it can act as the
    // user through the same-origin proxy and read the page it is sitting on.
    expect(directive(policyOf("/"), "script-src")).not.toContain("unsafe-inline");
    expect(directive(policyOf("/"), "script-src")).not.toContain("unsafe-eval");
  });

  it("allows inline style, knowingly, and only style", () => {
    // Next injects <style> during navigation and nonces are not reliably
    // applied to those. A stylesheet can deface and probe; it cannot execute.
    expect(directive(policyOf("/"), "style-src")).toContain("unsafe-inline");
  });

  it("allows same-origin connections and nothing else", () => {
    const connect = directive(policyOf("/"), "connect-src");

    // The BFF paid for this. The browser used to call the API on another
    // origin, so this directive had to name it; every request now goes to this
    // app's own /api/bff proxy, leaving no cross-origin destination to allow.
    expect(connect).toBe("connect-src 'self'");
  });

  it("cannot be framed, re-pointed, or made to post elsewhere", () => {
    const policy = policyOf("/");

    expect(policy).toContain("frame-ancestors 'none'");
    expect(policy).toContain("base-uri 'self'");
    expect(policy).toContain("form-action 'self'");
    expect(policy).toContain("object-src 'none'");
  });
});
