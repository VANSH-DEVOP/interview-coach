"""LangSmith tracing, and the decision not to send resume text to it.

A trace's payload is the prompt and the retrieved chunks. The prompt contains
resume text, and the chunks come from Chroma, which holds the resume
*unredacted* on purpose. So the default records shape rather than substance,
and the tests that matter are the ones proving no content escapes.
"""

import pytest

from app.core.config import get_settings
from app.services.ai import tracing
from app.services.ai.tracing import configure_tracing, summarise, traced

RESUME_LINE = "Priya Raman, priya@example.com, rebuilt the settlement ledger"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    configure_tracing.cache_clear()
    # The module forwards settings into os.environ for the SDK; monkeypatch
    # unwinds that so one test cannot switch tracing on for the next.
    for key in (
        "LANGSMITH_TRACING",
        "LANGSMITH_API_KEY",
        "LANGSMITH_PROJECT",
        "LANGSMITH_ENDPOINT",
    ):
        monkeypatch.delenv(key, raising=False)
    yield
    configure_tracing.cache_clear()


# -- Summarising ---------------------------------------------------------------


def test_a_string_becomes_its_length():
    assert summarise(RESUME_LINE) == f"<str {len(RESUME_LINE)} chars>"
    assert "priya@example.com" not in summarise(RESUME_LINE)


def test_numbers_and_booleans_survive():
    """Counts, scores and flags are the whole point of a shape-only trace."""
    assert summarise({"chunks": 3, "used_rag": True, "score": 0.42}) == {
        "chunks": 3,
        "used_rag": True,
        "score": 0.42,
    }


def test_a_list_becomes_a_count_and_a_sample_of_shapes():
    summary = summarise([RESUME_LINE] * 9)

    assert summary["count"] == 9
    assert all("chars>" in item for item in summary["sample"])
    assert RESUME_LINE not in str(summary)


def test_nesting_is_bounded():
    """A deeply nested structure must not become an unbounded walk on a path
    that runs inside every traced call."""
    deep: object = RESUME_LINE
    for _ in range(20):
        deep = {"inner": deep}

    assert "priya@example.com" not in str(summarise(deep))


def test_no_resume_text_survives_a_realistic_payload():
    payload = {
        "self": object(),
        "resume_text": RESUME_LINE,
        "chunks": [{"content": RESUME_LINE, "score": 0.5}],
        "spec": {"question_count": 5},
    }

    rendered = str(summarise(payload))

    assert "Priya" not in rendered
    assert "settlement" not in rendered
    assert "priya@example.com" not in rendered
    # The useful shape is still there.
    assert "'question_count': 5" in rendered


# -- Configuration -------------------------------------------------------------


def test_tracing_is_off_by_default():
    assert configure_tracing() is False


def test_tracing_stays_off_without_an_api_key(monkeypatch):
    """Half-configured is off, and says so, rather than failing later inside a
    request."""
    monkeypatch.setattr(get_settings(), "LANGSMITH_TRACING", True, raising=False)
    monkeypatch.setattr(get_settings(), "LANGSMITH_API_KEY", None, raising=False)

    assert configure_tracing() is False


def test_enabling_forwards_the_settings_to_the_sdk(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "LANGSMITH_TRACING", True, raising=False)
    monkeypatch.setattr(settings, "LANGSMITH_API_KEY", "ls-test-key", raising=False)
    monkeypatch.setattr(settings, "LANGSMITH_PROJECT", "test-project", raising=False)

    assert configure_tracing() is True

    import os

    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_PROJECT"] == "test-project"


# -- The decorator -------------------------------------------------------------


async def test_a_traced_function_is_untouched_when_tracing_is_off():
    """Zero cost in the default configuration: not a wrapper that does nothing,
    the original function itself."""

    async def original(value: int) -> int:
        return value * 2

    decorated = traced("noop")(original)

    assert decorated is original
    assert await decorated(21) == 42


async def test_a_traced_function_still_returns_its_value_when_tracing_is_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "LANGSMITH_TRACING", True, raising=False)
    monkeypatch.setattr(settings, "LANGSMITH_API_KEY", "ls-test-key", raising=False)
    # No network: the SDK batches to a background sender, and nothing here
    # asserts on delivery. What matters is that wrapping changes no behaviour.
    monkeypatch.setenv("LANGSMITH_TRACING_SAMPLING_RATE", "0")

    @traced("doubler")
    async def doubler(value: int) -> int:
        return value * 2

    assert await doubler(21) == 42


async def test_a_traced_function_still_raises_what_it_raised(monkeypatch):
    """Tracing must not swallow the failure it exists to record."""
    settings = get_settings()
    monkeypatch.setattr(settings, "LANGSMITH_TRACING", True, raising=False)
    monkeypatch.setattr(settings, "LANGSMITH_API_KEY", "ls-test-key", raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING_SAMPLING_RATE", "0")

    @traced("thrower")
    async def thrower() -> None:
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError, match="provider down"):
        await thrower()


def test_the_redactor_is_what_the_sdk_is_handed(monkeypatch):
    """The guard that keeps content out: the processors are passed unless the
    operator has explicitly opted in."""
    captured: dict[str, object] = {}

    def fake_traceable(**kwargs):
        captured.update(kwargs)
        return lambda function: function

    settings = get_settings()
    monkeypatch.setattr(settings, "LANGSMITH_TRACING", True, raising=False)
    monkeypatch.setattr(settings, "LANGSMITH_API_KEY", "ls-test-key", raising=False)
    monkeypatch.setattr(settings, "LANGSMITH_TRACE_CONTENT", False, raising=False)
    monkeypatch.setattr(tracing, "configure_tracing", lambda: True)

    import sys
    import types

    module = types.ModuleType("langsmith")
    module.traceable = fake_traceable  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langsmith", module)

    traced("thing")(lambda: None)

    assert captured["process_inputs"] is not None
    assert captured["process_outputs"] is not None
    # And what it does to a payload:
    assert "Priya" not in str(captured["process_inputs"]({"resume": RESUME_LINE}))
