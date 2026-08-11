"""In-process rate limiting.

Two things need protecting, for different reasons:

  * Auth endpoints, against credential stuffing. Keyed by client IP, since
    there is no authenticated user yet.
  * AI-backed endpoints, against quota burn. Every one costs a Gemini call,
    and the free tier allows 20 requests/day -- so a single enthusiastic user
    can exhaust the quota for everyone, after which the app silently serves
    deterministic fallback output to all users. Keyed by user id.

Fixed-window counters, held in Redis when there is one and in memory when there
is not. Which store is in play matters:

  * **Redis** -- one counter for the whole deployment. This is the only version
    that holds with more than one API replica, because the limits are about a
    *shared* resource: the Gemini account's daily quota, and one attacker's
    guesses against one account.
  * **In-process** -- per replica, so N replicas mean N x the intended ceiling.
    Correct for a single process (local development, tests) and the fallback
    when Redis is unreachable, where a limit that is N times too loose still
    beats no limit at all.

Fixed windows allow a burst of up to 2x the limit across a window boundary in
either store. Acceptable for abuse control, and much simpler than a sliding log
or leaky bucket.

Neither limitation weakens the property that matters here: an unauthenticated
client cannot make unbounded auth attempts, and one user cannot drain the
shared AI quota.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fastapi import Request

from app.core.config import get_settings
from app.core.exceptions import RateLimitedError

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# Prune expired keys once the table gets big enough to be worth the walk.
_PRUNE_THRESHOLD = 1024

# Namespace for the shared counters. arq owns `arq:*` in the same database.
_KEY_PREFIX = "ratelimit:"

# One round trip, and atomic, which INCR-then-EXPIRE from the client is not: a
# process that dies between the two leaves a key with no TTL, and that key locks
# its subject out permanently. Doing both inside the script removes the window.
#
# Returns the seconds until reset when the caller is over the limit, or -1 when
# it is allowed -- the same "None means fine" shape as the in-memory window.
_HIT_SCRIPT = """
local hits = redis.call('INCR', KEYS[1])
local ttl = redis.call('TTL', KEYS[1])
if ttl < 0 then
  -- First hit in this window, or (defensively) a key that somehow lost its
  -- expiry. Either way it gets one now, so the counter cannot outlive it.
  redis.call('EXPIRE', KEYS[1], ARGV[1])
  ttl = tonumber(ARGV[1])
end
if hits > tonumber(ARGV[2]) then
  return ttl
end
return -1
"""


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


@dataclass
class _Degradation:
    """Times Redis was there and still could not be counted against."""

    fallbacks: int = 0
    last_error: str | None = None
    last_at: str | None = None


_degradation = _Degradation()


def _record_fallback(error: BaseException) -> None:
    _degradation.fallbacks += 1
    _degradation.last_error = f"{type(error).__name__}: {error}"
    _degradation.last_at = datetime.now(timezone.utc).isoformat()
    logger.warning(
        "Rate-limit counter unavailable in Redis; counting in-process instead. "
        "The limit still applies, but per replica rather than per deployment. (%s)",
        _degradation.last_error,
        exc_info=error,
    )


def snapshot() -> dict[str, object]:
    """Current degradation state, for the health endpoint."""
    return {
        "fallbacks": _degradation.fallbacks,
        "last_error": _degradation.last_error,
        "last_at": _degradation.last_at,
    }


def reset() -> None:
    """Clear all rate-limit state. For tests."""
    global _degradation
    _window.reset()
    _degradation = _Degradation()


async def _hit_shared(
    redis: Redis, key: str, *, limit: int, window_seconds: int
) -> float | None:
    """`_Window.hit` against Redis. Returns seconds until reset, or None."""
    script = redis.register_script(_HIT_SCRIPT)
    # register_script hashes locally and sends EVALSHA, falling back to EVAL
    # only when Redis has not seen the script yet.
    retry_after = await script(keys=[_KEY_PREFIX + key], args=[window_seconds, limit])
    return None if int(retry_after) < 0 else float(retry_after)


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
    # A daily consumption cap rather than a burst guard. Counter-based on
    # purpose: an interview spends a provider call, and deleting the session
    # afterwards must not refund it.
    "interview_create": (
        "RATE_LIMIT_INTERVIEW_CREATES",
        "RATE_LIMIT_INTERVIEW_WINDOW_SECONDS",
    ),
}


async def enforce(key: str, *, scope: str, redis: Redis | None = None) -> None:
    """Count one request against `key`, and raise if it is over the limit.

    `redis` is the shared store; without one the counters are per-process, which
    is the intended design for a single process rather than a fault. A Redis
    that is *present and failing* degrades to the same in-process counters
    rather than raising: the alternative is either turning a Redis blip into a
    total auth outage, or waving every request through, and both are worse than
    a limit that is briefly too generous. It is recorded and reported at
    /health, so "briefly" stays checkable.
    """
    settings = get_settings()
    if not settings.RATE_LIMIT_ENABLED:
        return

    limit_attr, window_attr = _LIMITS[scope]
    limit: int = getattr(settings, limit_attr)
    window_seconds: int = getattr(settings, window_attr)
    scoped_key = f"{scope}:{key}"

    retry_after: float | None = None
    if redis is not None:
        try:
            retry_after = await _hit_shared(
                redis, scoped_key, limit=limit, window_seconds=window_seconds
            )
        except Exception as exc:  # noqa: BLE001 - any Redis failure degrades
            _record_fallback(exc)
            redis = None

    if redis is None:
        retry_after = _window.hit(scoped_key, limit=limit, window_seconds=window_seconds)

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
