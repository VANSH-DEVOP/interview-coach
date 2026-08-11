"""Error reporting, and the resume text that must not ride along with it.

Two things are being pinned here. One is that reporting is wired to the places
this codebase actually fails -- the deliberate swallow points, not the ASGI
layer, because an application with 42 `except Exception` blocks lets almost
nothing reach the ASGI layer.

The other is what leaves the process. A crash payload contains whatever crashed
it, and here that is a prompt built from a resume or a transcript of answers.
These tests assert on the scrubbed event, not on the configuration that is
supposed to produce it, because a `send_default_pii=False` that is set and then
undone by an integration would pass every other kind of test.
"""

import logging

import pytest

from app.core import error_reporting
from app.core.config import get_settings
from app.core.error_reporting import _before_send, _scrub, configure_error_reporting, report

RESUME = "Priya Raman, priya@example.com, rebuilt the settlement ledger"


@pytest.fixture(autouse=True)
def _clean_state():
    configure_error_reporting.cache_clear()
    yield
    configure_error_reporting.cache_clear()


@pytest.fixture
def _content_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "SENTRY_SEND_CONTENT", False, raising=False)


# -- Scrubbing ------------------------------------------------------------------


def test_a_string_becomes_its_length() -> None:
    assert _scrub(RESUME) == f"<str {len(RESUME)} chars>"
    assert "priya@example.com" not in _scrub(RESUME)


def test_counts_and_flags_survive() -> None:
    """Shape is the whole point of a scrubbed report."""
    assert _scrub({"chunks": 3, "used_rag": True, "score": 0.42}) == {
        "chunks": 3,
        "used_rag": True,
        "score": 0.42,
    }


def test_keys_that_name_content_are_redacted_outright() -> None:
    scrubbed = _scrub({"prompt": RESUME, "transcript": ["a"], "attempt": 2})

    assert scrubbed["prompt"] == "<redacted>"
    assert scrubbed["transcript"] == "<redacted>"
    assert scrubbed["attempt"] == 2


def test_a_count_survives_a_key_that_sounds_like_content() -> None:
    """`chunks: 3` is a count, not a chunk. Redacting it would throw away the
    diagnosis to protect an integer -- and these counters are most of what the
    report is for."""
    scrubbed = _scrub({"chunks": 9, "chunks_embedded": 4, "prompt": RESUME})

    assert scrubbed["chunks"] == 9
    assert scrubbed["chunks_embedded"] == 4
    assert scrubbed["prompt"] == "<redacted>"


def test_nesting_is_bounded() -> None:
    deep: object = RESUME
    for _ in range(20):
        deep = {"inner": deep}

    assert "priya@example.com" not in str(_scrub(deep))


# -- The event that actually leaves --------------------------------------------


def test_local_variables_are_stripped_from_frames(_content_off) -> None:
    """The single richest source of resume text in a crash: the locals at the
    frame where generation failed."""
    event = {
        "exception": {
            "values": [
                {
                    "stacktrace": {
                        "frames": [
                            {"function": "generate_json", "vars": {"prompt": RESUME}}
                        ]
                    }
                }
            ]
        }
    }

    sent = _before_send(event, {})

    assert "vars" not in sent["exception"]["values"][0]["stacktrace"]["frames"][0]
    assert "Priya" not in str(sent)


def test_the_request_body_and_query_string_are_dropped(_content_off) -> None:
    """A query string has carried an API key in this project before."""
    event = {
        "request": {
            "url": "https://example.test/api/v1/interviews",
            "data": {"answer": RESUME},
            "query_string": "key=secret-value",
            "cookies": {"ip_access_token": "abc"},
        }
    }

    sent = _before_send(event, {})

    assert sent["request"]["url"].endswith("/interviews")
    assert "data" not in sent["request"]
    assert "query_string" not in sent["request"]
    assert "cookies" not in sent["request"]
    assert "secret-value" not in str(sent)


def test_breadcrumb_data_is_scrubbed(_content_off) -> None:
    event = {"breadcrumbs": {"values": [{"message": "rag.retrieval", "data": {"chunk": RESUME}}]}}

    sent = _before_send(event, {})

    assert "Priya" not in str(sent)


def test_extra_context_is_scrubbed(_content_off) -> None:
    event = {"extra": {"resume_text": RESUME, "chunks": 9}}

    sent = _before_send(event, {})

    assert "settlement" not in str(sent)
    # The useful shape survives.
    assert sent["extra"]["chunks"] == 9


def test_an_empty_event_passes_through(_content_off) -> None:
    """The scrubber runs on every event, including ones with none of these
    sections; a KeyError here would drop the report it is meant to protect."""
    assert _before_send({}, {}) == {}


def test_opting_in_returns_the_event_untouched(monkeypatch) -> None:
    """`SENTRY_SEND_CONTENT=true` is for a self-hosted instance, and it says so
    in the setting. What it must not do is half-work."""
    monkeypatch.setattr(get_settings(), "SENTRY_SEND_CONTENT", True, raising=False)
    event = {"extra": {"resume_text": RESUME}}

    assert _before_send(event, {})["extra"]["resume_text"] == RESUME


# -- Configuration --------------------------------------------------------------


def test_reporting_is_off_without_a_dsn() -> None:
    assert configure_error_reporting() is False


def test_report_is_a_no_op_when_off() -> None:
    """No DSN means no SDK call and no cost -- and, more importantly, no
    exception from the reporting path itself."""
    report(RuntimeError("provider down"), operation="initial_questions")


def test_reporting_never_raises_at_the_caller(monkeypatch) -> None:
    """Every call site is a place that caught an exception and carried on. A
    reporter that raises there would turn a handled fault into an outage."""
    import sys
    import types

    monkeypatch.setattr(error_reporting, "configure_error_reporting", lambda: True)

    broken = types.ModuleType("sentry_sdk")

    def _explode(*args, **kwargs):
        raise RuntimeError("sentry is down")

    broken.new_scope = _explode  # type: ignore[attr-defined]
    broken.capture_exception = _explode  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentry_sdk", broken)

    # No raise: the caller has already handled its own failure.
    report(RuntimeError("provider down"), operation="initial_questions")


# -- End to end, through the real SDK -------------------------------------------


@pytest.fixture
def captured_events(monkeypatch):
    """Run the real `configure_error_reporting`, but keep the events here.

    Asserting on the event the SDK actually builds, rather than on the kwargs
    we passed it, is the point: `include_local_variables=False` that some
    integration re-enables would satisfy a configuration test and leak resume
    text in production.
    """
    import sentry_sdk

    events: list[dict] = []

    class _Capture(sentry_sdk.Transport):
        def capture_envelope(self, envelope):
            for item in envelope.items:
                if item.headers.get("type") == "event":
                    events.append(item.payload.json)

    real_init = sentry_sdk.init

    def _init_with_capture(**kwargs):
        return real_init(**{**kwargs, "transport": _Capture()})

    monkeypatch.setattr(sentry_sdk, "init", _init_with_capture)
    monkeypatch.setattr(
        get_settings(), "SENTRY_DSN", "https://key@example.invalid/1", raising=False
    )
    monkeypatch.setattr(get_settings(), "SENTRY_SEND_CONTENT", False, raising=False)

    assert configure_error_reporting() is True
    yield events
    sentry_sdk.flush()


def test_a_crash_reports_the_stack_without_the_resume(captured_events) -> None:
    """The whole point, end to end: the frame that failed is reported, and the
    resume text that was sitting in it is not."""

    def generate_json():
        # The real names from gemini_client, so this fails the way that would.
        prompt = RESUME  # noqa: F841
        system_instruction = "Be rigorous."  # noqa: F841
        raise RuntimeError("provider down")

    try:
        generate_json()
    except RuntimeError as exc:
        report(exc, operation="initial_questions", context={"attempt": 1})

    import sentry_sdk

    sentry_sdk.flush()

    assert len(captured_events) == 1
    event = captured_events[0]
    blob = str(event)

    # The diagnosis survives.
    assert event["exception"]["values"][0]["type"] == "RuntimeError"
    assert "generate_json" in blob
    assert event["tags"]["operation"] == "initial_questions"
    # The resume does not.
    assert "Priya" not in blob
    assert "priya@example.com" not in blob
    assert "settlement" not in blob


def test_an_error_log_becomes_an_event(captured_events) -> None:
    """Where coverage of the 42 `except Exception` blocks actually comes from.

    Most of them log at ERROR before swallowing, so the reporting follows
    without a call at each site -- and keeps following when someone adds the
    forty-third.
    """
    logging.getLogger("app.services.test").error("Failed to index resume %s", "abc-123")

    import sentry_sdk

    sentry_sdk.flush()

    assert any("Failed to index resume" in str(event) for event in captured_events)


def test_an_info_log_does_not(captured_events) -> None:
    """INFO is breadcrumbs, not events. Otherwise every retrieval trace this
    application emits would be an issue."""
    logging.getLogger("app.services.test").info("rag.retrieval")

    import sentry_sdk

    sentry_sdk.flush()

    assert not [event for event in captured_events if "rag.retrieval" in str(event)]


# -- Where it is wired ----------------------------------------------------------


def test_a_fallback_is_reported(monkeypatch) -> None:
    """The choke point every AI degradation passes through.

    Reported at warning, not error: one fallback is the system working. It is
    fingerprinted so a quota-exhausted afternoon is one issue, not three
    hundred pages.
    """
    from app.services.ai import degradation

    captured: list[dict] = []
    monkeypatch.setattr(
        degradation,
        "report",
        lambda error, **kwargs: captured.append({"error": error, **kwargs}),
    )

    degradation.record_fallback("initial_questions", RuntimeError("HTTP 429"))

    assert len(captured) == 1
    assert captured[0]["operation"] == "initial_questions"
    assert captured[0]["level"] == "warning"
