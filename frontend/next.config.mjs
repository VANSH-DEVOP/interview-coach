/** @type {import('next').NextConfig} */

/**
 * Security headers that are the same for every response live here rather than
 * in middleware.ts, for one reason: these also apply to `/_next/static/*`,
 * which the middleware matcher deliberately skips. The Content-Security-Policy
 * is the exception and stays in middleware, because it carries a per-request
 * nonce and cannot be a constant.
 */
const securityHeaders = [
  // Stops a browser second-guessing our Content-Type -- the sniffing that
  // turns an uploaded file served back as text into a script.
  { key: "X-Content-Type-Options", value: "nosniff" },
  // Superseded by the CSP's frame-ancestors, kept for what still reads it.
  { key: "X-Frame-Options", value: "DENY" },
  // Paths here contain session and report ids. Send the origin to other sites
  // and nothing at all when leaving HTTPS.
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  // Features this app does not use, denied to the page and to anything it
  // embeds.
  //
  // `microphone=(self)` is the exception, and it is not optional: dictation
  // uses the Web Speech API, which Chrome gates behind exactly this policy.
  // With `microphone=()` the browser refuses before it ever prompts, so the
  // button reported "microphone access was blocked" and allowing it in site
  // settings changed nothing -- the denial was this header, not the user.
  //
  // The comment here previously said this "has to change if voice interviews
  // land". They landed, and it did not.
  {
    key: "Permissions-Policy",
    value:
      "accelerometer=(), autoplay=(), camera=(), display-capture=(), " +
      "encrypted-media=(), geolocation=(), gyroscope=(), magnetometer=(), " +
      "microphone=(self), midi=(), payment=(), usb=()",
  },
  // Severs window.opener, so a page opened from here cannot reach back in.
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  { key: "X-Permitted-Cross-Domain-Policies", value: "none" },
];

const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
  // Next advertises its version to every visitor by default, which is a free
  // hint about which CVEs to try.
  poweredByHeader: false,
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

export default nextConfig;
