"""What retrieval actually did, per call and in aggregate.

Retrieval is the one AI path that fails *quietly and usefully*: when it finds
nothing, or breaks, or was never enabled, the generator falls back to the first
4000 characters of the resume and produces perfectly plausible questions. The
interview still works. It is simply no longer personalised, and nothing
anywhere says so -- `degradation.py` counts provider failures, and none of
these are provider failures.

That is the same shape as the retired-embedding-model incident: a subsystem off
for weeks behind output that looked fine. So before changing how retrieval
works, make it say what it is doing.

Three things get recorded:

- **Availability.** RAG disables itself when `CHROMA_PATH` is unwritable, which
  outside Docker is the common case. That decision is made once at startup and
  logged once; here it stays readable at /health for the life of the process.
- **Retrievals.** Outcome, latency, how many chunks came back and how close the
  best one was. `hit` is not success: five chunks at distance 1.9 are five
  irrelevant chunks, and the distances are what will show that.
- **Indexing.** Chunks produced versus chunks actually embedded, since a
  partially-embedded resume retrieves partial answers for ever afterwards.

Process-local and unsynchronised, like `degradation.py`: a diagnostic signal,
not billing. The counters reset when the process does.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Literal

logger = logging.getLogger(__name__)

# What happened to one retrieval attempt.
Outcome = Literal[
    # Chunks came back.
    "hit",
    # Retrieval ran and the index held nothing for this resume -- usually a
    # resume uploaded before indexing worked, or one whose embedding failed.
    "empty",
    # Retrieval raised. The caller falls back to raw resume text.
    "failed",
]

Purpose = Literal["initial_questions", "follow_up", "eval"]


@dataclass
class _State:
    # Availability, decided once when the service is built.
    enabled: bool | None = None
    disabled_reason: str | None = None

    retrievals: int = 0
    hits: int = 0
    empty: int = 0
    failed: int = 0
    # Times the generator used truncated resume text instead of retrieved
    # context. Deliberately counted at the *generator*, not here: retrieval
    # returning nothing and retrieval never being asked both land in the same
    # degraded prompt, and only the generator sees both.
    full_text_fallbacks: int = 0

    total_ms: float = 0.0
    max_ms: float = 0.0
    # Distance of the best chunk on the most recent hit. Chroma cosine
    # distance: 0 is identical, 2 is opposite.
    last_best_distance: float | None = None
    last_at: str | None = None

    resumes_indexed: int = 0
    chunks_produced: int = 0
    chunks_embedded: int = 0

    # Hybrid retrieval. `dense_only` and `sparse_only` climbing together means
    # the two halves are finding different things, which is the case for
    # running both; `agreed` at zero means they never corroborate each other
    # and one of them is probably misconfigured.
    fusions: int = 0
    dense_only: int = 0
    sparse_only: int = 0
    agreed: int = 0

    # Embedding cache. `cache_errors` is counted apart from `cache_misses`
    # because an unreachable cache behaves exactly like a cold one -- the
    # request still succeeds, at full provider cost -- and reading the two as
    # one number is how you keep paying while believing the cache works.
    cache_hits: int = 0
    cache_misses: int = 0
    cache_errors: int = 0

    recent: list[dict[str, object]] = field(default_factory=list)


_state = _State()

# Enough recent traces to see a pattern, few enough to stay cheap. This is a
# ring buffer, not a log: the log lines are the durable record.
_RECENT_LIMIT = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_availability(*, enabled: bool, reason: str | None = None) -> None:
    """Note whether retrieval is switched on at all, and why not if it isn't."""
    _state.enabled = enabled
    _state.disabled_reason = None if enabled else reason


def record_retrieval(
    *,
    purpose: Purpose,
    outcome: Outcome,
    duration_ms: float,
    chunks: int = 0,
    best_distance: float | None = None,
    error: str | None = None,
) -> None:
    """Record one retrieval attempt, and log it as one structured line."""
    _state.retrievals += 1
    if outcome == "hit":
        _state.hits += 1
    elif outcome == "empty":
        _state.empty += 1
    else:
        _state.failed += 1

    _state.total_ms += duration_ms
    _state.max_ms = max(_state.max_ms, duration_ms)
    _state.last_at = _now()
    if best_distance is not None:
        _state.last_best_distance = best_distance

    trace: dict[str, object] = {
        "purpose": purpose,
        "outcome": outcome,
        "duration_ms": round(duration_ms, 1),
        "chunks": chunks,
    }
    if best_distance is not None:
        trace["best_distance"] = round(best_distance, 4)
    if error:
        trace["error"] = error

    _state.recent.append({"at": _state.last_at, **trace})
    del _state.recent[:-_RECENT_LIMIT]

    # One line per retrieval, with the numbers as fields rather than prose, so
    # "how often does this return nothing" is a query rather than an afternoon.
    logger.info("rag.retrieval", extra={"rag": trace})


def record_full_text_fallback(*, purpose: Purpose, reason: str) -> None:
    """The generator used truncated resume text instead of retrieved context."""
    _state.full_text_fallbacks += 1
    logger.info(
        "rag.full_text_fallback",
        extra={"rag": {"purpose": purpose, "reason": reason}},
    )


def record_cache(*, outcome: Literal["hit", "miss", "error"]) -> None:
    """Record one embedding-cache lookup."""
    if outcome == "hit":
        _state.cache_hits += 1
    elif outcome == "miss":
        _state.cache_misses += 1
    else:
        _state.cache_errors += 1


def record_fusion(
    *, purpose: Purpose, dense: int, sparse: int, fused: int, agreed: int
) -> None:
    """Record what each half of hybrid retrieval contributed.

    The interesting number is `agreed`: chunks both retrievers ranked. Zero
    across many retrievals means the two halves never corroborate each other,
    which is what a broken embedding model or an empty keyword index looks like
    from the outside -- the fused list still comes back full, from one source.
    """
    _state.fusions += 1
    if dense and not sparse:
        _state.dense_only += 1
    elif sparse and not dense:
        _state.sparse_only += 1
    _state.agreed += agreed

    logger.info(
        "rag.fusion",
        extra={
            "rag": {
                "purpose": purpose,
                "dense": dense,
                "sparse": sparse,
                "fused": fused,
                "agreed": agreed,
            }
        },
    )


def record_indexing(*, chunks_produced: int, chunks_embedded: int, duration_ms: float) -> None:
    """Record one resume indexing pass."""
    _state.resumes_indexed += 1
    _state.chunks_produced += chunks_produced
    _state.chunks_embedded += chunks_embedded
    _state.last_at = _now()

    logger.info(
        "rag.indexed",
        extra={
            "rag": {
                "chunks_produced": chunks_produced,
                "chunks_embedded": chunks_embedded,
                # Below 1.0 means some chunks are missing from the index and
                # every later retrieval on this resume is working blind.
                "embedded_ratio": (
                    round(chunks_embedded / chunks_produced, 3) if chunks_produced else 0.0
                ),
                "duration_ms": round(duration_ms, 1),
            }
        },
    )


@contextmanager
def timed() -> Iterator[list[float]]:
    """Measure a block in milliseconds.

    Yields a one-element list so the caller can read the elapsed time after the
    block, including when the block raised.
    """
    elapsed = [0.0]
    started = time.perf_counter()
    try:
        yield elapsed
    finally:
        elapsed[0] = (time.perf_counter() - started) * 1000


def snapshot() -> dict[str, object]:
    """Current retrieval state, for the health endpoint."""
    return {
        "enabled": _state.enabled,
        "disabled_reason": _state.disabled_reason,
        "retrievals": _state.retrievals,
        "hits": _state.hits,
        "empty": _state.empty,
        "failed": _state.failed,
        "full_text_fallbacks": _state.full_text_fallbacks,
        "avg_ms": round(_state.total_ms / _state.retrievals, 1) if _state.retrievals else None,
        "max_ms": round(_state.max_ms, 1) if _state.retrievals else None,
        "last_best_distance": _state.last_best_distance,
        "resumes_indexed": _state.resumes_indexed,
        "chunks_produced": _state.chunks_produced,
        "chunks_embedded": _state.chunks_embedded,
        "fusions": _state.fusions,
        "dense_only": _state.dense_only,
        "sparse_only": _state.sparse_only,
        "agreed": _state.agreed,
        "cache_hits": _state.cache_hits,
        "cache_misses": _state.cache_misses,
        "cache_errors": _state.cache_errors,
        "last_at": _state.last_at,
    }


def recent() -> list[dict[str, object]]:
    """The last few retrieval traces, newest last."""
    return list(_state.recent)


def reset() -> None:
    """Clear the recorded state. For tests."""
    global _state
    _state = _State()
