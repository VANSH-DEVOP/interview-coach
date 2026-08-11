"""Error reporting to Sentry, for a codebase that deliberately swallows errors.

`degradation.py` and `call_metrics.py` say *how often* something failed.
Neither says what the traceback was, and by the time a rate looks wrong the
failure is hours old. This is the other half: the individual exception, with a
stack, grouped so a hundred instances of one fault read as one fault.

**The load-bearing decision is where reporting is wired in, not that it exists.**
There are 42 `except Exception` blocks in this application and that is the
design, not an accident: an interview must always be completable, so every AI
path, the queue, the cache and the rate limiter catch everything and carry on.
A reporter attached only to the ASGI middleware would therefore be nearly
silent -- it would see the handful of faults that escape a system built so that
faults do not escape, and miss every one the fallbacks absorb. Which is to say
it would be quietest exactly when things are worst.

So two routes in, on purpose:

- **Log records at ERROR and above become events.** Most of those 42 blocks
  already log at that level before swallowing, so the coverage comes free and
  stays correct when someone adds the forty-third.
- **`report()` is called explicitly from `record_fallback()`**, the one choke
  point every AI degradation passes through. Those are reported at *warning*,
  and fingerprinted by operation and exception type, because a single fallback
  is the system working: a quota-exhausted afternoon should be one issue that
  says "429, 300 times", not three hundred pages.

**Content is scrubbed, and that is the other decision.** A crash payload
contains what crashed it -- and here that is a prompt built from a resume, or a
transcript of answers. Sentry's defaults capture request bodies and every local
variable in every frame, which is precisely `prompt`, `resume_text` and
`transcript` at the moment they matter. That would hand a third party the data
`masking.py` exists to withhold, silently, because nobody reviews a crash report
the way they review a request. Same conclusion as `tracing.py`, reached the same
way, and the settings say what they cost.

**This is not a guarantee, and the gap is worth naming.** Log messages and
exception strings are reported as written, because scrubbing them would delete
the diagnosis -- "Failed to index resume abc-123: 429" is the whole value of
the report. Almost all of them are format strings from this repository with an
id interpolated, which is fine. The exception is an exception: a provider error
this application wraps (`EmbeddingError(f"...: {exc}")`) could in principle echo
part of the request back in its message. So the rule for anyone adding an
`except` here is the one `masking.py` already states -- log the tally, the id or
the type, never the text.

Disabled by default: with no DSN this module configures nothing and `report()`
returns immediately.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Literal

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Event fields that carry request or response bodies. Removed wholesale rather
# than inspected: this application's bodies are resume text and answers.
_DROPPED_REQUEST_KEYS = ("data", "cookies")

# Substrings marking a context value as content rather than shape. Matched on
# the key, because the values are the thing we must not look at too closely --
# a rule that decides by inspecting resume text has already read it.
_SENSITIVE_KEY_PARTS = (
    "answer",
    "chunk",
    "content",
    "context",
    "document",
    "email",
    "password",
    "prompt",
    "question",
    "resume",
    "text",
    "token",
    "transcript",
)

_REDACTED = "<redacted>"

# Sentry's severity vocabulary, mirrored so the signature below stays checkable
# without importing the SDK at module scope.
Level = Literal["fatal", "critical", "error", "warning", "info", "debug"]


def _looks_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _scrub(value: Any, _depth: int = 0) -> Any:
    """Replace anything that could be user content with a description of it.

    Deliberately a *separate* implementation from `tracing.summarise` despite
    doing a similar job: that one lives in `app/services/ai/`, and `app/core/`
    importing from a service would invert the layering this project enforces
    everywhere else. The duplication is six lines; the inversion would be
    permanent.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return f"<str {len(value)} chars>"
    if _depth >= 3:
        return type(value).__name__
    if isinstance(value, dict):
        return {str(key): _entry(str(key), item, _depth) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        return {"count": len(items), "sample": [_scrub(i, _depth + 1) for i in items[:3]]}
    return type(value).__name__


def _entry(key: str, value: Any, _depth: int) -> Any:
    """One key/value pair, with the key's name taken as a hint about the value.

    Numbers and flags survive whatever the key is called: `chunks: 3` is a
    count, not a chunk, and redacting it would throw away the diagnosis to
    protect an integer. The name only decides the fate of things that could
    *hold* text -- where it upgrades `<str 900 chars>` to `<redacted>`.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if _looks_sensitive(key):
        return _REDACTED
    return _scrub(value, _depth + 1)


def _before_send(event: Any, hint: Any) -> Any:
    """Strip user content from an event just before it leaves the process.

    The last line of defence rather than the only one: local variables and
    request bodies are already switched off in `configure_error_reporting`.
    This catches what a future SDK version, or an integration we did not
    anticipate, decides to attach anyway.
    """
    settings = get_settings()
    if settings.SENTRY_SEND_CONTENT:
        return event

    request = event.get("request")
    if isinstance(request, dict):
        for key in _DROPPED_REQUEST_KEYS:
            request.pop(key, None)
        # Query strings have carried an API key in this project before.
        request.pop("query_string", None)

    # `extra` only, and deliberately not `tags` or `contexts`.
    #
    # `extra` is the arbitrary bag: the logging integration empties every
    # non-standard field of a log record into it, so it is the one section that
    # can contain anything this application ever passed to `extra=`.
    #
    # Tags are short labels we or the SDK set -- the operation name, the
    # environment -- and they are what makes an issue findable; scrubbing them
    # turns `operation: initial_questions` into `<str 17 chars>` and protects
    # nothing, because no user text is ever put there. Contexts are the SDK's
    # runtime and OS blocks, plus ours, which `report()` has already scrubbed
    # at the source.
    if isinstance(event.get("extra"), dict):
        event["extra"] = _scrub(event["extra"])

    # Belt and braces: locals are disabled at init, but if a frame arrives with
    # them anyway they are the single richest source of resume text here.
    for exception in (event.get("exception") or {}).get("values") or []:
        for frame in (exception.get("stacktrace") or {}).get("frames") or []:
            frame.pop("vars", None)

    for crumb in (event.get("breadcrumbs") or {}).get("values") or []:
        if isinstance(crumb, dict) and isinstance(crumb.get("data"), dict):
            crumb["data"] = _scrub(crumb["data"])

    return event


@lru_cache(maxsize=1)
def configure_error_reporting() -> bool:
    """Initialise Sentry from our settings. Returns whether it is on.

    Cached, so importing this from both the API and the worker is safe and the
    SDK is initialised once per process.
    """
    settings = get_settings()
    if not settings.SENTRY_DSN:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:  # pragma: no cover - depends on install extras
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed.")
        return False

    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.ENVIRONMENT,
        release=settings.SENTRY_RELEASE,
        # Performance tracing stays off here. The AI pipeline is already traced
        # by LangSmith, which understands what a retrieval and a generation are;
        # a second tracer would double the vendors seeing this traffic to
        # produce a worse picture of it.
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        # Never on. This is Sentry's own switch for IP addresses, usernames and
        # request bodies, and every one of those is something this application
        # is careful about elsewhere.
        send_default_pii=False,
        # The important one. Local variables at the point of a crash in this
        # codebase are `prompt`, `resume_text`, `transcript`, `answer` -- the
        # exact values masking.py exists to keep out of third-party hands.
        include_local_variables=settings.SENTRY_SEND_CONTENT,
        max_request_body_size="always" if settings.SENTRY_SEND_CONTENT else "never",
        before_send=_before_send,
        integrations=[
            LoggingIntegration(
                # Breadcrumbs: the trail leading up to the fault.
                level=logging.INFO,
                # Events: what actually gets reported. ERROR and above, which is
                # what the swallow points already log before carrying on, so
                # this covers them without a call at each site.
                event_level=logging.ERROR,
            )
        ],
    )
    logger.info(
        "Sentry error reporting enabled (environment=%s, content=%s).",
        settings.ENVIRONMENT,
        "full" if settings.SENTRY_SEND_CONTENT else "scrubbed",
    )
    return True


def report(
    error: BaseException,
    *,
    operation: str,
    level: Level = "error",
    context: dict[str, Any] | None = None,
) -> None:
    """Report one handled exception that would otherwise vanish.

    For the deliberate swallow points. Everything here is caught on purpose and
    the caller carries on, so this must never raise and never block: a reporter
    that breaks a request is worse than no reporter.

    Args:
        operation: What was being attempted, e.g. "initial_questions". Also the
            grouping key, so one fault does not arrive as a hundred issues.
        level: "warning" for a degradation the system absorbed, "error" for one
            it did not.
        context: Extra fields. Scrubbed like everything else -- pass counts and
            identifiers, never text.
    """
    if not configure_error_reporting():
        return

    try:
        import sentry_sdk

        with sentry_sdk.new_scope() as scope:
            scope.set_level(level)
            scope.set_tag("operation", operation)
            # Group by what failed and where, not by the message. A quota storm
            # produces three hundred exceptions whose messages differ only in a
            # retry-after value; without this they are three hundred issues.
            scope.fingerprint = [operation, type(error).__name__]
            if context:
                scope.set_context("operation", _scrub(context))
            sentry_sdk.capture_exception(error)
    except Exception:  # noqa: BLE001 - reporting must not break the caller
        logger.debug("Could not report an error to Sentry.", exc_info=True)
