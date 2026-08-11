"""What each provider call cost, and how often the provider answered at all.

`degradation.py` counts the moments an AI path gave up and served a
deterministic answer instead. That is the alert that matters, but on its own it
is only a numerator: three fallbacks is a catastrophe against thirty calls and a
rounding error against three thousand, and until now nothing counted the calls.

This module is the denominator, plus the two numbers you want once the rate
looks wrong -- **latency** and **token spend**.

**Recorded at the transport, not at the call sites.** The same reasoning that
put redaction in `GeminiClient` and `EmbeddingService`: measured here, a new
call site cannot forget to be measured, because it never has to remember. It
also means the latency recorded is the provider's, not ours -- the chunking,
fusion and prompt assembly around it are somebody else's numbers.

**Only the network round-trip is inside the measurement.** A reply that arrives
and then fails to parse is recorded here as a call that succeeded, because it
did: the provider answered, it took that long, it cost those tokens. That the
answer was unusable shows up as a *fallback* in `degradation.py`. Two numbers,
two questions -- "is the provider reachable" and "is its output usable" -- and
collapsing them would hide the case where the provider is up and the output is
garbage, which is what a changed response schema looks like.

**Embedding calls carry no token counts.** Google's embedding endpoint does not
return usage, so `input_tokens` is null for them rather than zero -- a zero
would average into the token figures and quietly understate spend. It matters
less than it sounds: the binding constraint on the free tier is twenty
*requests* a day, not tokens, and requests are counted here exactly.

Process-local and unsynchronised, like `degradation.py` and
`retrieval_metrics.py`. This is a diagnostic signal, not an invoice: the
counters reset when the process does, and several replicas each keep their own.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Literal

logger = logging.getLogger(__name__)

# The provider calls this application makes. Both cost a request against the
# same daily quota, which is why they are counted together and broken down
# rather than tracked in separate modules.
Operation = Literal["generate", "embed"]

# Enough recent failures to see whether they are all the same failure, few
# enough to stay cheap. The log lines are the durable record.
_RECENT_ERRORS_LIMIT = 5


@dataclass
class _OperationState:
    calls: int = 0
    ok: int = 0
    failed: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0
    # None until a call reports usage, so an operation that never reports it
    # (embeddings) stays distinguishable from one that reported zero.
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class _State:
    operations: dict[str, _OperationState] = field(default_factory=dict)
    last_model: str | None = None
    last_error: str | None = None
    last_at: str | None = None
    recent_errors: list[dict[str, object]] = field(default_factory=list)

    def for_operation(self, operation: str) -> _OperationState:
        return self.operations.setdefault(operation, _OperationState())


_state = _State()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def usage_of(reply: Any) -> tuple[int | None, int | None]:
    """Input and output token counts from a chat reply, if it carries them.

    Read defensively rather than trusted. `usage_metadata` is a LangChain
    convention that each integration fills in as it sees fit, and a provider
    that stops populating it must cost us a null in a metric, not an
    AttributeError in the middle of generating an interview.
    """
    usage = getattr(reply, "usage_metadata", None)
    if not isinstance(usage, dict):
        return None, None

    def _count(key: str) -> int | None:
        value = usage.get(key)
        return value if isinstance(value, int) else None

    return _count("input_tokens"), _count("output_tokens")


def record_call(
    *,
    operation: Operation,
    model: str,
    outcome: Literal["ok", "failed"],
    duration_ms: float,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    error: str | None = None,
) -> None:
    """Record one round-trip to the provider, and log it as one structured line."""
    stats = _state.for_operation(operation)
    stats.calls += 1
    if outcome == "ok":
        stats.ok += 1
    else:
        stats.failed += 1

    stats.total_ms += duration_ms
    stats.max_ms = max(stats.max_ms, duration_ms)
    if input_tokens is not None:
        stats.input_tokens = (stats.input_tokens or 0) + input_tokens
    if output_tokens is not None:
        stats.output_tokens = (stats.output_tokens or 0) + output_tokens

    _state.last_model = model
    _state.last_at = _now()

    trace: dict[str, object] = {
        "operation": operation,
        "model": model,
        "outcome": outcome,
        "duration_ms": round(duration_ms, 1),
    }
    if input_tokens is not None:
        trace["input_tokens"] = input_tokens
    if output_tokens is not None:
        trace["output_tokens"] = output_tokens

    if outcome == "failed":
        _state.last_error = error
        trace["error"] = error
        _state.recent_errors.append({"at": _state.last_at, **trace})
        del _state.recent_errors[:-_RECENT_ERRORS_LIMIT]
        # WARNING, unlike a successful call: a provider that is failing is the
        # thing this module exists to make visible, and it should not need a
        # debug log level to be seen.
        logger.warning("ai.call", extra={"ai": trace})
    else:
        logger.info("ai.call", extra={"ai": trace})


@dataclass
class _Call:
    """Handle for the block being measured, so it can report what it learned."""

    input_tokens: int | None = None
    output_tokens: int | None = None

    def usage(self, reply: Any) -> None:
        """Take token counts from the provider's reply, if it carries any."""
        self.input_tokens, self.output_tokens = usage_of(reply)


@contextmanager
def measure(operation: Operation, model: str) -> Iterator[_Call]:
    """Time one provider round-trip and record it, however it ends.

    Re-raises whatever the block raised. Recording a failure must not change
    what the caller sees: everything above here is written against
    `GeminiError` / `EmbeddingError`, and the fallback layer keys on them.
    """
    call = _Call()
    started = time.perf_counter()
    try:
        yield call
    except BaseException as exc:
        record_call(
            operation=operation,
            model=model,
            outcome="failed",
            duration_ms=(time.perf_counter() - started) * 1000,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    else:
        record_call(
            operation=operation,
            model=model,
            outcome="ok",
            duration_ms=(time.perf_counter() - started) * 1000,
            input_tokens=call.input_tokens,
            output_tokens=call.output_tokens,
        )


def _summarise(stats: _OperationState) -> dict[str, object]:
    return {
        "calls": stats.calls,
        "ok": stats.ok,
        "failed": stats.failed,
        "failure_rate": round(stats.failed / stats.calls, 3) if stats.calls else None,
        "avg_ms": round(stats.total_ms / stats.calls, 1) if stats.calls else None,
        "max_ms": round(stats.max_ms, 1) if stats.calls else None,
        "input_tokens": stats.input_tokens,
        "output_tokens": stats.output_tokens,
    }


def snapshot() -> dict[str, object]:
    """Current provider-call state, for the health endpoint.

    Totals first, per-operation underneath. The totals answer "is the provider
    healthy"; the breakdown answers "which half of it isn't", which matters
    because generation and embedding fail for different reasons and one retired
    embedding model already hid behind a working chat model here.
    """
    calls = sum(stats.calls for stats in _state.operations.values())
    failed = sum(stats.failed for stats in _state.operations.values())
    total_ms = sum(stats.total_ms for stats in _state.operations.values())
    tokens = [
        (stats.input_tokens, stats.output_tokens) for stats in _state.operations.values()
    ]
    input_tokens = [value for value, _ in tokens if value is not None]
    output_tokens = [value for _, value in tokens if value is not None]

    return {
        "calls": calls,
        "ok": calls - failed,
        "failed": failed,
        "failure_rate": round(failed / calls, 3) if calls else None,
        "avg_ms": round(total_ms / calls, 1) if calls else None,
        "max_ms": (
            round(max(stats.max_ms for stats in _state.operations.values()), 1)
            if calls
            else None
        ),
        # Null rather than zero when nothing reported usage, so "no token data"
        # and "no tokens spent" stay different answers.
        "input_tokens": sum(input_tokens) if input_tokens else None,
        "output_tokens": sum(output_tokens) if output_tokens else None,
        "last_model": _state.last_model,
        "last_error": _state.last_error,
        "last_at": _state.last_at,
        "by_operation": {
            operation: _summarise(stats)
            for operation, stats in sorted(_state.operations.items())
        },
    }


def recent_errors() -> list[dict[str, object]]:
    """The last few failed calls, newest last."""
    return list(_state.recent_errors)


def reset() -> None:
    """Clear the recorded state. For tests."""
    global _state
    _state = _State()
