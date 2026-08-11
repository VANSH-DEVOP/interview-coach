import type { Metadata } from "next";

import "@/styles/globals.css";

/**
 * Every route is rendered per request, and the CSP is why.
 *
 * `src/middleware.ts` puts a fresh nonce in the Content-Security-Policy on each
 * response, and Next stamps that nonce onto the inline scripts it emits for
 * hydration. A prerendered page's inline scripts are baked at build time and
 * carry the *build's* nonce -- which is to say none -- so a per-request nonce
 * can never match them and `strict-dynamic` blocks all seven of them. The
 * symptom is a blank page, and it appears only in a real browser: the HTML is
 * served with status 200 and looks perfect to curl.
 *
 * Measured before choosing: this turns 11 prerendered routes into
 * server-rendered ones. They are auth-gated shells that fetch their data
 * client-side, and this deploys as one container with no CDN, so what is
 * actually lost is re-rendering a static shell per request. The alternative was
 * `script-src 'unsafe-inline'`. That was originally justified by tokens being
 * readable from JavaScript; the httpOnly BFF has since removed that, and the
 * decision was re-checked rather than left standing on a premise that expired.
 * An injected script can no longer steal the session, but it can still act as
 * the user through the same-origin proxy and read whatever is on the page --
 * so the policy still earns a cost that remains close to nothing.
 *
 * Revisit if a CDN appears in front of this, which is the one change that would
 * make the prerendering genuinely worth something.
 */
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: {
    default: "InterviewPilot AI",
    template: "%s | InterviewPilot AI",
  },
  description: "AI-powered interview preparation platform for professionals.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen">{children}</body>
    </html>
  );
}
