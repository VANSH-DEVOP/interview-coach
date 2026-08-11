"""Process-local record of AI fallback activations.

Every AI path degrades to a deterministic local implementation when the
provider fails. That is the right behaviour, but it is invisible: a retired
model, a revoked key, and "the questions feel generic" all look identical from
the outside. A retired embedding model went unnoticed here for exactly that
reason.

This module is the seam where a degradation becomes observable. It logs at
WARNING and keeps a counter that `/health` reports, so a non-zero fallback rate
is something you can see and alert on.

Deliberately in-process and unsynchronised: it is a diagnostic signal, not
billing. Replace with real metrics when telemetry lands.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class _State:
    # Operations that reached a real provider implementation, whether or not it
    # worked. Without this, `fallbacks` is a numerator with nothing under it:
    # three fallbacks is a catastrophe against thirty attempts and a rounding
    # error against three thousand.
    attempts: int = 0
    fallbacks: int = 0
    last_operation: str | None = None
    last_error: str | None = None
    last_at: str | None = None


_state = _State()


def record_attempt(operation: str) -> None:
    """Note that an AI operation was tried against the real provider.

    Counted at the fallback wrappers rather than at the transport, because this
    is the denominator for *operations that could degrade*, not for provider
    round-trips -- one `initial_questions` may spend two calls (generate, then
    a corrective refine) and is still one thing that either worked or came back
    generic. Provider round-trips are counted in `call_metrics.py`.

    When no API key is configured there is no wrapper and nothing is counted,
    which is right: nothing was attempted, so there is no rate to report.
    """
    _state.attempts += 1


def record_fallback(operation: str, error: BaseException) -> None:
    """Log and count one degradation to the deterministic fallback.

    Args:
        operation: The AI operation that failed, e.g. "initial_questions".
        error: The provider exception being swallowed.
    """
    _state.fallbacks += 1
    _state.last_operation = operation
    _state.last_error = f"{type(error).__name__}: {error}"
    _state.last_at = datetime.now(timezone.utc).isoformat()

    logger.warning(
        "AI provider failed during %s; falling back to the deterministic "
        "implementation. Output will be generic. (%s)",
        operation,
        _state.last_error,
        exc_info=error,
    )


def snapshot() -> dict[str, object]:
    """Current degradation state, for the health endpoint."""
    return {
        "attempts": _state.attempts,
        "fallbacks": _state.fallbacks,
        # Null, not zero, when nothing has been attempted. Zero would read as
        # "the provider is healthy" on a deployment that has no API key and has
        # never called it.
        "fallback_rate": (
            round(_state.fallbacks / _state.attempts, 3) if _state.attempts else None
        ),
        "last_operation": _state.last_operation,
        "last_error": _state.last_error,
        "last_at": _state.last_at,
    }


def reset() -> None:
    """Clear the recorded state. For tests."""
    global _state
    _state = _State()
