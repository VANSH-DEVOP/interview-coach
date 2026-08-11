"""LangSmith tracing for the AI pipeline.

`retrieval_metrics` answers "how often does retrieval come back empty". It
cannot answer "why was *this* interview's third question generic", because the
log lines for one interview's rewrite, dense search, keyword search, fusion,
prompt and parse are not tied together by anything. Tracing gives that: one
span tree per operation, with timings and errors attached to the step that
produced them.

**Content is not traced by default, and that is the important decision.** A
trace's inputs and outputs are the prompt and the retrieved chunks; the prompt
contains resume text, and the chunks come from Chroma, which deliberately holds
the resume *unredacted* ("Chroma is ours; Google is not" -- see masking.py).
Shipping those to a hosted service would hand a third party precisely the data
`masking.py` exists to withhold, and would do it silently, because a trace is
not a request anyone reviews.

So the default records shape rather than substance: how many chunks, how long,
what failed, string lengths instead of strings. That still gives the span tree,
the latency waterfall and the exception, which is most of what "what do I
tackle" needs. `LANGSMITH_TRACE_CONTENT=true` opts into full payloads for local
debugging or a self-hosted instance, and the setting says what it costs.

Disabled by default. With tracing off, `traced` returns the function untouched
-- no wrapper, no per-call cost, nothing to reason about in production until
someone deliberately turns it on.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from functools import lru_cache
from typing import Any, Literal, TypeVar

from app.core.config import get_settings

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# The span kinds LangSmith understands. Mirrored rather than imported so the
# signature below stays checkable without the SDK installed.
RunType = Literal["tool", "chain", "llm", "retriever", "embedding", "prompt", "parser"]

# Beyond this a summarised container tells you nothing more than its size.
_MAX_SUMMARISED_ITEMS = 5


@lru_cache(maxsize=1)
def configure_tracing() -> bool:
    """Point the LangSmith SDK at our settings. Returns whether it is on.

    This is the one module that writes to `os.environ`, against the rule that
    every variable enters through `app/core/config.py`. The SDK reads its
    configuration from the environment and offers no other way in, so the
    choice is between this and a second source of truth. The values still come
    from `Settings`; this only forwards them.
    """
    settings = get_settings()
    if not settings.LANGSMITH_TRACING:
        return False
    if not settings.LANGSMITH_API_KEY:
        logger.warning(
            "LANGSMITH_TRACING is on but LANGSMITH_API_KEY is unset; tracing stays off."
        )
        return False

    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.LANGSMITH_API_KEY
    os.environ["LANGSMITH_PROJECT"] = settings.LANGSMITH_PROJECT
    if settings.LANGSMITH_ENDPOINT:
        os.environ["LANGSMITH_ENDPOINT"] = settings.LANGSMITH_ENDPOINT

    logger.info(
        "LangSmith tracing enabled (project=%s, content=%s).",
        settings.LANGSMITH_PROJECT,
        "full" if settings.LANGSMITH_TRACE_CONTENT else "shape only",
    )
    return True


def summarise(value: Any, _depth: int = 0) -> Any:
    """Describe a value's shape without reproducing it.

    Strings become their length, containers become their size and a sample of
    summarised members. Nothing that could be resume text, an answer or a
    prompt survives.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return f"<str {len(value)} chars>"
    if _depth >= 3:
        return type(value).__name__
    if isinstance(value, dict):
        return {str(key): summarise(item, _depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        return {
            "count": len(items),
            "sample": [summarise(item, _depth + 1) for item in items[:_MAX_SUMMARISED_ITEMS]],
        }
    return type(value).__name__


def _redact(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: summarise(value) for key, value in payload.items()}


def traced(name: str, *, run_type: RunType = "chain") -> Callable[[F], F]:
    """Trace a coroutine as one span, when tracing is configured on.

    Returns the function unchanged when tracing is off, so the decorator costs
    nothing at all in the default configuration -- no wrapper frame, no import
    of the SDK at call time, no behaviour to explain when something goes wrong
    in production.
    """

    def decorate(function: F) -> F:
        if not configure_tracing():
            return function

        from langsmith import traceable

        settings = get_settings()
        if settings.LANGSMITH_TRACE_CONTENT:
            # Opted in: full prompts and chunks, including resume text.
            return traceable(run_type=run_type, name=name)(function)  # type: ignore[return-value]
        return traceable(  # type: ignore[return-value]
            run_type=run_type,
            name=name,
            process_inputs=_redact,
            process_outputs=lambda outputs: _redact(
                outputs if isinstance(outputs, dict) else {"output": outputs}
            ),
        )(function)

    return decorate
