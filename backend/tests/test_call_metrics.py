"""Provider-call telemetry: latency, token spend, and the failure denominator.

The distinction these pin down is the one that is easy to collapse and
expensive to lose: a call that *arrives* and a call that is *usable* are
different events. `call_metrics` counts the first, `degradation` counts the
second, and a provider that answers promptly with garbage shows up as healthy
in one and broken in the other. That is what a changed response schema looks
like, and merging the two numbers would hide it.
"""

import pytest

from app.services.ai import call_metrics
from app.services.ai.call_metrics import measure, record_call, snapshot, usage_of


@pytest.fixture(autouse=True)
def _clean_state():
    call_metrics.reset()
    yield
    call_metrics.reset()


class _Reply:
    """A chat reply carrying LangChain's usage convention."""

    def __init__(self, usage: object) -> None:
        self.usage_metadata = usage


# -- Counting -------------------------------------------------------------------


def test_nothing_called_reports_no_rate_rather_than_a_perfect_one() -> None:
    """Zero would read as "the provider is healthy" on a deployment that has
    never called it."""
    state = snapshot()

    assert state["calls"] == 0
    assert state["failure_rate"] is None
    assert state["avg_ms"] is None
    assert state["input_tokens"] is None


def test_the_failure_rate_is_the_point() -> None:
    for _ in range(3):
        record_call(operation="generate", model="m", outcome="ok", duration_ms=10.0)
    record_call(
        operation="generate", model="m", outcome="failed", duration_ms=10.0, error="boom"
    )

    state = snapshot()
    assert (state["calls"], state["ok"], state["failed"]) == (4, 3, 1)
    assert state["failure_rate"] == 0.25


def test_operations_are_broken_out_as_well_as_totalled() -> None:
    """A retired embedding model already hid behind a working chat model here."""
    record_call(operation="generate", model="chat", outcome="ok", duration_ms=10.0)
    record_call(
        operation="embed", model="emb", outcome="failed", duration_ms=10.0, error="404"
    )

    state = snapshot()
    assert state["failure_rate"] == 0.5
    by_operation = state["by_operation"]
    assert by_operation["generate"]["failure_rate"] == 0.0
    assert by_operation["embed"]["failure_rate"] == 1.0


# -- Latency --------------------------------------------------------------------


def test_latency_is_averaged_and_peaked() -> None:
    """The average hides the timeout; the max is where it shows."""
    record_call(operation="generate", model="m", outcome="ok", duration_ms=10.0)
    record_call(operation="generate", model="m", outcome="ok", duration_ms=30.0)

    state = snapshot()
    assert state["avg_ms"] == 20.0
    assert state["max_ms"] == 30.0


def test_measure_times_a_block_and_records_it() -> None:
    with measure("generate", "m"):
        pass

    state = snapshot()
    assert state["calls"] == 1
    assert state["failed"] == 0
    assert state["avg_ms"] is not None


# -- Tokens ---------------------------------------------------------------------


def test_usage_is_taken_from_the_reply() -> None:
    with measure("generate", "m") as call:
        call.usage(_Reply({"input_tokens": 120, "output_tokens": 45}))

    state = snapshot()
    assert state["input_tokens"] == 120
    assert state["output_tokens"] == 45


def test_a_reply_without_usage_costs_a_null_not_an_error() -> None:
    """`usage_metadata` is a convention each integration fills in as it likes.
    A provider that stops populating it must cost a metric, not an interview."""
    assert usage_of(_Reply(None)) == (None, None)
    assert usage_of(_Reply("not a dict")) == (None, None)
    assert usage_of(object()) == (None, None)
    assert usage_of(_Reply({"input_tokens": "many"})) == (None, None)


def test_calls_that_report_no_usage_leave_tokens_null_rather_than_zero() -> None:
    """Embedding calls return no usage. A zero would average into the token
    figures and understate spend; null says "no data" instead."""
    record_call(operation="embed", model="emb", outcome="ok", duration_ms=5.0)

    state = snapshot()
    assert state["calls"] == 1
    assert state["input_tokens"] is None
    assert state["by_operation"]["embed"]["input_tokens"] is None


# -- Failures -------------------------------------------------------------------


def test_measure_records_a_failure_and_re_raises_it() -> None:
    """Recording must not change what the caller sees: everything above is
    written against ModelError/EmbeddingError and the fallback layer keys on
    them."""
    with pytest.raises(RuntimeError, match="provider down"):
        with measure("generate", "m"):
            raise RuntimeError("provider down")

    state = snapshot()
    assert state["failed"] == 1
    assert state["failure_rate"] == 1.0
    assert "provider down" in str(state["last_error"])


def test_recent_errors_are_kept_and_bounded() -> None:
    for index in range(8):
        with pytest.raises(RuntimeError):
            with measure("generate", "m"):
                raise RuntimeError(f"failure {index}")

    errors = call_metrics.recent_errors()
    assert len(errors) == 5
    # Newest last, oldest dropped.
    assert "failure 7" in str(errors[-1]["error"])
    assert "failure 0" not in str(errors)


# -- The boundary ---------------------------------------------------------------
#
# The guarantee is not that these numbers can be recorded, but that they are --
# at the transport, so a new call site cannot forget to be measured because it
# never has to remember. Same reasoning that put redaction there, and the same
# reason these assert on a stubbed provider rather than on a helper.


@pytest.fixture
def chat_transport(monkeypatch):
    """Stands in for ChatGoogleGenerativeAI at the last point we control."""

    class _Chat:
        async def ainvoke(self, messages):
            class _Reply:
                content = '{"ok": true}'
                usage_metadata = {"input_tokens": 11, "output_tokens": 7}

            return _Reply()

    from app.services.ai import model_client as model_module

    monkeypatch.setattr(model_module.ModelClient, "_model_client", lambda self: _Chat())


@pytest.fixture
def embed_transport(monkeypatch):
    """Stands in for GoogleGenerativeAIEmbeddings. Returns no usage, as Google
    does not."""

    class _Embeddings:
        async def aembed_query(self, text):
            return [0.1, 0.2, 0.3]

    from app.services.ai import embedding as embedding_module

    monkeypatch.setattr(
        embedding_module.EmbeddingService, "_model_client", lambda self: _Embeddings()
    )


async def test_a_generation_call_is_measured(chat_transport) -> None:
    from app.services.ai.model_client import ModelClient

    await ModelClient("k", "gemini-test").generate_json(
        system_instruction="Be rigorous.", prompt="hello"
    )

    state = snapshot()
    assert state["by_operation"]["generate"]["calls"] == 1
    assert state["failed"] == 0
    assert state["last_model"] == "gemini-test"
    # Token spend, taken from the reply rather than estimated.
    assert state["input_tokens"] == 11
    assert state["output_tokens"] == 7


async def test_an_embedding_call_is_measured(embed_transport) -> None:
    from app.services.ai.embedding import EmbeddingService

    await EmbeddingService("k", model="embed-test").embed_text("hello")

    state = snapshot()
    assert state["by_operation"]["embed"]["calls"] == 1
    assert state["last_model"] == "embed-test"
    # Counted as a request, which is the quota that binds, with no token data.
    assert state["input_tokens"] is None


async def test_a_dead_provider_is_counted_as_a_failed_call(monkeypatch) -> None:
    from app.services.ai import model_client as model_module
    from app.services.ai.model_client import ModelClient, ModelError

    class _Dead:
        async def ainvoke(self, messages):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(model_module.ModelClient, "_model_client", lambda self: _Dead())

    with pytest.raises(ModelError):
        await ModelClient("k", "m").generate_json(system_instruction="s", prompt="p")

    state = snapshot()
    assert state["failed"] == 1
    assert "connection refused" in str(state["last_error"])


async def test_a_reply_that_will_not_parse_is_still_a_successful_call(
    monkeypatch,
) -> None:
    """The distinction the two metrics exist to keep apart.

    The provider answered: it was reachable, it took that long, it cost those
    tokens. That the body was unusable is a *fallback*, recorded elsewhere.
    Collapsing the two would hide "provider up, output garbage" -- which is
    exactly the shape of a changed response schema.
    """
    from app.services.ai import model_client as model_module
    from app.services.ai.model_client import ModelClient, ModelError

    class _Babbling:
        async def ainvoke(self, messages):
            class _Reply:
                content = "not json at all"

            return _Reply()

    monkeypatch.setattr(
        model_module.ModelClient, "_model_client", lambda self: _Babbling()
    )

    with pytest.raises(ModelError):
        await ModelClient("k", "m").generate_json(system_instruction="s", prompt="p")

    state = snapshot()
    assert state["calls"] == 1
    assert state["failed"] == 0, "a parse failure is not a transport failure"
