"""In-process rate limiting.

Two things need protecting, for different reasons:

  * Auth endpoints, against credential stuffing. Keyed by client IP, since
    there is no authenticated user yet.
  * AI-backed endpoints, against quota burn. Every one costs a Gemini call,
    and the free tier allows 20 requests/day -- so a single enthusiastic user
    can exhaust the quota for everyone, after which the app silently serves
    deterministic fallback output to all users. Keyed by user id.

Fixed-window counters, held in memory. Two deliberate limitations:

  * Per-process. With multiple workers or replicas the effective limit is
    multiplied by the worker count. Moving to Redis is the fix; the seam is
    the _Window class, which is the only thing that touches storage.
  * Fixed windows allow a burst of up to 2x the limit across a window
    boundary. Acceptable for abuse control, and much simpler than a sliding
    log or leaky bucket.

Neither limitation weakens the property that matters here: an unauthenticated
client cannot make unbounded auth attempts, and one user cannot drain the
shared AI quota.
"""

from __future__ import annotations

import logging
import time

from fastapi import Request

from app.core.config import get_settings
from app.core.exceptions import RateLimitedError

logger = logging.getLogger(__name__)

# Prune expired keys once the table gets big enough to be worth the walk.
_PRUNE_THRESHOLD = 1024


class _Window:
    """Fixed-window hit counter keyed by an opaque string."""

    def __init__(self) -> None:
        # key -> (window_expiry_monotonic, hits_in_window)
        self._hits: dict[str, tuple[float, int]] = {}

    def hit(self, key: str, *, limit: int, window_seconds: int) -> float | None:
        """Record a hit. Returns seconds until reset if the caller is over.

        No awaits inside, so under asyncio this is effectively atomic and
        needs no lock.
        """
        now = time.monotonic()
        entry = self._hits.get(key)

        if entry is None or now >= entry[0]:
            if len(self._hits) >= _PRUNE_THRESHOLD:
                self._prune(now)
            self._hits[key] = (now + window_seconds, 1)
            return None

        expires_at, count = entry
        if count >= limit:
            return max(expires_at - now, 0.0)

        self._hits[key] = (expires_at, count + 1)
        return None

    def _prune(self, now: float) -> None:
        expired = [k for k, (expires_at, _) in self._hits.items() if now >= expires_at]
        for key in expired:
            del self._hits[key]

    def reset(self) -> None:
        """Clear all counters. For tests."""
        self._hits.clear()


_window = _Window()


def reset() -> None:
    """Clear all rate-limit state. For tests."""
    _window.reset()


def client_ip(request: Request) -> str:
    """Best-effort client address.

    X-Forwarded-For is trusted only because this app is expected to sit behind
    a proxy that sets it. If it is ever exposed directly, a client can spoof
    the header and bypass IP limits -- terminate TLS behind a proxy that
    overwrites the header.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# scope -> (limit setting, window setting). Resolved per request rather than at
# import, so tests and deployments can change limits without reimporting.
_LIMITS: dict[str, tuple[str, str]] = {
    "auth": ("RATE_LIMIT_AUTH_ATTEMPTS", "RATE_LIMIT_AUTH_WINDOW_SECONDS"),
    "ai": ("RATE_LIMIT_AI_REQUESTS", "RATE_LIMIT_AI_WINDOW_SECONDS"),
    "upload": ("RATE_LIMIT_UPLOAD_REQUESTS", "RATE_LIMIT_UPLOAD_WINDOW_SECONDS"),
}


def enforce(key: str, *, scope: str) -> None:
    settings = get_settings()
    if not settings.RATE_LIMIT_ENABLED:
        return

    limit_attr, window_attr = _LIMITS[scope]
    limit: int = getattr(settings, limit_attr)
    window_seconds: int = getattr(settings, window_attr)

    retry_after = _window.hit(
        f"{scope}:{key}", limit=limit, window_seconds=window_seconds
    )
    if retry_after is None:
        return

    logger.warning(
        "Rate limit hit: scope=%s key=%s limit=%d/%ds", scope, key, limit, window_seconds
    )
    raise RateLimitedError(
        f"Too many requests. Try again in {int(retry_after) + 1} seconds.",
        retry_after=int(retry_after) + 1,
    )


# The FastAPI dependencies that call enforce() live in app/api/deps.py, with
# the rest of the DI wiring. This module stays a pure mechanism: no knowledge
# of routes, users, or authentication.
