"""Security response headers for the API.

This process serves JSON, not pages, which changes what is worth setting and
what is theatre. A `Content-Security-Policy` protects a *rendering* context, and
almost nothing here renders -- so the policy below is the maximally restrictive
one (`default-src 'none'`) rather than a tuned allowlist, and its job is to make
the two cases that *do* render inert: an error page a proxy or framework
substitutes, and any future endpoint that returns HTML by accident.

The real page-level CSP belongs to the frontend, which is where scripts execute
and where the nonce lives. See `frontend/middleware.ts`.

Two headers here are deliberately conditional:

- **HSTS is production-only.** It is a promise the browser remembers for a year,
  and it applies to a *host*, not a response. Sent from a staging or local box
  sharing a parent domain -- with `includeSubDomains`, from anywhere under it --
  it can lock a domain into HTTPS long after the box is gone. `preload` is
  deliberately absent: that submits the domain to a list baked into browsers,
  which is a decision to make on purpose and not a side effect of a header
  default.
- **`/docs` gets a looser policy.** Swagger UI is real HTML that loads its
  bundle from a CDN and initialises itself with an inline script, so
  `default-src 'none'` would render a blank page. It only exists off production
  (`docs_url=None` there), so the loosening never reaches a deployed site.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# The API's own policy. Every fetch directive falls back to `default-src`, so
# `'none'` covers scripts, styles, images, frames and connections at once.
#
#   frame-ancestors  -- nothing may embed a response from this origin.
#   base-uri         -- a <base> tag cannot re-point relative URLs.
#   form-action      -- an injected form cannot post anywhere.
#
# `frame-ancestors` is what X-Frame-Options became; both are sent because the
# older header is what some corporate proxies and scanners still read.
_API_CSP = (
    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)

# Swagger UI's bundle and stylesheet come from jsdelivr, it initialises through
# an inline script, and FastAPI points the favicon at its own site.
_DOCS_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://fastapi.tiangolo.com; "
    "font-src 'self' https://cdn.jsdelivr.net; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)

_ONE_YEAR = 60 * 60 * 24 * 365

# Features this application has no use for. An empty allowlist denies the
# feature to the page and to everything it embeds.
#
# `microphone=()` stays denied here and is granted only by the *frontend*
# (next.config.mjs), which is where dictation runs. This process serves JSON;
# a permissions policy on an API response governs no page and grants nothing.
_PERMISSIONS_POLICY = (
    "accelerometer=(), autoplay=(), camera=(), display-capture=(), "
    "encrypted-media=(), fullscreen=(), geolocation=(), gyroscope=(), "
    "magnetometer=(), microphone=(), midi=(), payment=(), usb=()"
)

_HEADERS = {
    # Stops a browser second-guessing our Content-Type. Without it a JSON
    # response containing attacker-chosen text can be sniffed as HTML and run.
    "X-Content-Type-Options": "nosniff",
    # Superseded by frame-ancestors, kept for what still reads it.
    "X-Frame-Options": "DENY",
    # API paths carry resource ids. `no-referrer` keeps them out of the
    # Referer header of anything a response leads to.
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": _PERMISSIONS_POLICY,
    # Severs the window.opener relationship, so a page opened from here cannot
    # reach back into it.
    "Cross-Origin-Opener-Policy": "same-origin",
    # There is no crossdomain.xml here, and this says so rather than leaving
    # the question to a legacy plugin's default.
    "X-Permitted-Cross-Domain-Policies": "none",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach security headers to every response.

    Args:
        hsts: Send Strict-Transport-Security. Production only -- see the module
            docstring for why this is not simply always on.
        docs_paths: Paths that render HTML and need the looser policy. Empty by
            default, so the strict policy is what a caller gets by forgetting.
            `create_app` passes the app's *actual* `docs_url`, which is `None`
            in production -- so the CDN-permitting policy does not exist there
            at all, rather than being applied to the 404 that replaces the page.
    """

    def __init__(
        self,
        app,
        *,
        hsts: bool = False,
        docs_paths: tuple[str, ...] = (),
    ) -> None:
        super().__init__(app)
        self._hsts = hsts
        self._docs_paths = docs_paths

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        for header, value in _HEADERS.items():
            # setdefault, not assignment: a route that has deliberately set one
            # of these knows something this middleware does not.
            response.headers.setdefault(header, value)

        is_docs = any(request.url.path.startswith(path) for path in self._docs_paths)
        response.headers.setdefault(
            "Content-Security-Policy", _DOCS_CSP if is_docs else _API_CSP
        )

        if self._hsts:
            response.headers.setdefault(
                "Strict-Transport-Security",
                f"max-age={_ONE_YEAR}; includeSubDomains",
            )

        return response
