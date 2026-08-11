/**
 * Two jobs, both per-request.
 *
 * 1. Route protection: redirects unauthenticated users away from app pages and
 *    authenticated users away from auth pages. Authorization is enforced
 *    server-side by the API; this is a UX guard only.
 *
 * 2. Content-Security-Policy, with a fresh nonce per response.
 *
 * The nonce is the reason this file runs on every route rather than only the
 * protected ones. Next.js reads the nonce out of the CSP header on the
 * *request* and stamps it onto the scripts it injects, so the header has to be
 * set before the page renders -- there is no way to add it afterwards.
 *
 * **This costs static prerendering, and that was the trade.** A per-request
 * nonce opts a route out of being prerendered: before this change 11 of 14
 * routes were static, and they are now server-rendered on demand. It was worth
 * it here for two specific reasons, and both should be re-checked if either
 * changes:
 *
 *   - The alternative is `script-src 'unsafe-inline'`, which Next.js needs for
 *     its hydration scripts otherwise. That defeats the one thing CSP is really
 *     for. And the tokens live in JS-readable cookies (see api-client.ts, and
 *     the httpOnly BFF item in goals.md), so an injected script does not merely
 *     deface a page -- it takes the session. `unsafe-inline` would leave the
 *     policy pointed away from the attack that matters most here.
 *   - The routes it costs are auth-gated shells that fetch their data
 *     client-side, and this deploys as a single container with no CDN in front.
 *     Rendering the same shell per request is close to free; there was no edge
 *     cache to lose. On a CDN, or with real static content, weigh it again.
 */

import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

const PROTECTED_PREFIXES = ["/dashboard", "/profile", "/resumes", "/interviews", "/reports"];
const AUTH_PAGES = ["/login", "/register"];

/**
 * The API origin, so `connect-src` can allow it. The frontend and the API are
 * separate origins in every environment including local (`:3000` and `:8000`),
 * so omitting this blocks every request the app makes.
 *
 * Falls back to allowing only same-origin rather than to allowing anything: a
 * misconfigured build should fail visibly in the console, not quietly widen the
 * policy.
 */
function apiOrigin(): string {
  const url = process.env.NEXT_PUBLIC_API_URL;
  if (!url) return "";
  try {
    return new URL(url).origin;
  } catch {
    return "";
  }
}

function contentSecurityPolicy(nonce: string): string {
  const directives = [
    "default-src 'self'",
    // 'strict-dynamic' lets a nonced script load the chunks it needs without
    // every chunk URL being listed. Browsers that understand it ignore the
    // host allowlist entirely, which is the point -- an allowlist is what
    // attackers walk around.
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'`,
    // 'unsafe-inline' for styles, knowingly. Tailwind ships a stylesheet, but
    // Next injects inline <style> during navigation and styled-jsx emits more,
    // and nonces on styles are not reliably applied to either. An injected
    // stylesheet can deface and can probe, but it cannot execute, so this is a
    // far smaller concession than the same word in script-src.
    "style-src 'self' 'unsafe-inline'",
    // blob: and data: are for locally previewed resume uploads.
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    [`connect-src 'self'`, apiOrigin()].filter(Boolean).join(" "),
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    // No <frame>/<iframe> anywhere in this app.
    "frame-src 'none'",
    "worker-src 'self' blob:",
    "manifest-src 'self'",
  ];
  return directives.join("; ");
}

export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const hasSession = Boolean(request.cookies.get("ip_refresh_token")?.value);

  const isProtected = PROTECTED_PREFIXES.some((p) => pathname.startsWith(p));
  const isAuthPage = AUTH_PAGES.some((p) => pathname.startsWith(p));

  // crypto.randomUUID is available in the edge runtime; 16 bytes of base64 is
  // well past the 128 bits the CSP spec asks for. A nonce that repeats is a
  // nonce an attacker can guess and reuse, so this must be per response and
  // never derived from anything about the request.
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const csp = contentSecurityPolicy(nonce);

  // Redirects still happen, and still get the headers: a redirect carries a
  // body in some browsers, and leaving one response unprotected is how a gap
  // gets found.
  if (isProtected && !hasSession) {
    const loginUrl = new URL("/login", request.url);
    loginUrl.searchParams.set("next", pathname);
    return withSecurityHeaders(NextResponse.redirect(loginUrl), csp);
  }

  if (isAuthPage && hasSession) {
    return withSecurityHeaders(NextResponse.redirect(new URL("/dashboard", request.url)), csp);
  }

  // The CSP goes on the *request* as well as the response. That is how Next
  // finds the nonce to put on its own <script> tags; x-nonce is for our own
  // components, which can read it with headers().
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("content-security-policy", csp);

  return withSecurityHeaders(
    NextResponse.next({ request: { headers: requestHeaders } }),
    csp
  );
}

function withSecurityHeaders(response: NextResponse, csp: string): NextResponse {
  response.headers.set("content-security-policy", csp);
  return response;
}

export const config = {
  matcher: [
    /*
     * Every path except Next's own immutable build output and the static file
     * conventions. Those are subresources -- the CSP that governs them is the
     * one on the document that pulled them in -- and running middleware for
     * each would add a hop per asset.
     *
     * Broadening this from the old five-prefix list is safe for the redirect
     * logic above, which decides from `pathname` and falls through to
     * `next()` for anything it does not recognise.
     */
    "/((?!_next/static|_next/image|favicon.ico|robots.txt|sitemap.xml).*)",
  ],
};
