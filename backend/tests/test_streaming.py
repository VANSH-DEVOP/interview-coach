"""Streaming, and the two places it deliberately does not go.

`stream_json` exists to return *the same thing* `generate_json` returns — parsed
JSON, `ModelError` on failure — while handing fragments to a caller as they
arrive. Anything weaker would mean the streaming path had quietly worse
guarantees than the buffered one, which is how a "faster" code path becomes the
one that ships broken output.

The scope is structural, not effort. `initial_questions` runs
generate → critique → refine, so streaming it would show a candidate questions
that are then rewritten or trimmed under them. Evaluation is queued to a worker
and nobody is watching. `follow_up` is one call with a user waiting on it, and
is the only path wired up.
"""

import pytest

from app.services.ai import call_metrics
from app.services.ai.model_client import ModelClient, ModelError


@pytest.fixture(autouse=True)
def _clean_metrics():
    call_metrics.reset()
    yield
    call_metrics.reset()


class _Chunk:
    """An AIMessageChunk as LangChain yields it."""

    def __init__(self, text: str, usage: dict | None = None) -> None:
        self.content = text
        self.usage_metadata = usage


def _streaming_client(chunks, monkeypatch, **kwargs) -> ModelClient:
    class _Model:
        def astream(self, messages):
            async def _gen():
                for chunk in chunks:
                    yield chunk

            return _gen()

    from app.services.ai import model_client as module

    monkeypatch.setattr(module.ModelClient, "_model_client", lambda self: _Model())
    return ModelClient("k", "streamer", **kwargs)


# -- The contract ---------------------------------------------------------------


async def test_fragments_are_reassembled_and_parsed(monkeypatch):
    client = _streaming_client(
        [_Chunk('{"ask_follow_up"'), _Chunk(': true, "content"'), _Chunk(': "Why?"}')],
        monkeypatch,
    )

    result = await client.stream_json(system_instruction="s", prompt="p")

    assert result == {"ask_follow_up": True, "content": "Why?"}


async def test_a_fenced_stream_still_parses(monkeypatch):
    """The provider without a JSON mode is also the one most likely to be
    streamed, so the two features have to compose."""
    client = _streaming_client(
        [_Chunk("```json\n"), _Chunk('{"ok": true}'), _Chunk("\n```")], monkeypatch
    )

    assert await client.stream_json(system_instruction="s", prompt="p") == {"ok": True}


async def test_chunks_reach_the_caller_in_order(monkeypatch):
    seen: list[str] = []
    client = _streaming_client(
        [_Chunk('{"a"'), _Chunk(": 1"), _Chunk("}")], monkeypatch
    )

    await client.stream_json(
        system_instruction="s", prompt="p", on_chunk=seen.append
    )

    assert seen == ['{"a"', ": 1", "}"]


async def test_an_async_on_chunk_is_awaited(monkeypatch):
    """An SSE writer is a coroutine, so both shapes have to work."""
    seen: list[str] = []

    async def collect(text: str) -> None:
        seen.append(text)

    client = _streaming_client([_Chunk('{"a": 1}')], monkeypatch)

    await client.stream_json(system_instruction="s", prompt="p", on_chunk=collect)

    assert seen == ['{"a": 1}']


async def test_no_partial_json_is_ever_emitted(monkeypatch):
    """Fragments go to `on_chunk` as text; the parse happens once, at the end.
    Half an object is not a smaller answer, it is an invalid one."""
    parsed: list = []

    def record(text: str) -> None:
        parsed.append(text)

    client = _streaming_client(
        [_Chunk('{"score"'), _Chunk(": 7}")], monkeypatch
    )
    result = await client.stream_json(
        system_instruction="s", prompt="p", on_chunk=record
    )

    assert parsed == ['{"score"', ": 7}"]  # raw text, not objects
    assert result == {"score": 7}


async def test_an_unparseable_stream_raises_the_same_error_as_the_buffered_path(
    monkeypatch,
):
    client = _streaming_client([_Chunk("I cannot help with that.")], monkeypatch)

    with pytest.raises(ModelError, match="response shape"):
        await client.stream_json(system_instruction="s", prompt="p")


async def test_a_provider_failure_mid_stream_becomes_a_model_error(monkeypatch):
    class _Model:
        def astream(self, messages):
            async def _gen():
                yield _Chunk('{"a"')
                raise RuntimeError("connection reset")

            return _gen()

    from app.services.ai import model_client as module

    monkeypatch.setattr(module.ModelClient, "_model_client", lambda self: _Model())

    with pytest.raises(ModelError, match="stream failed"):
        await ModelClient("k", "m").stream_json(system_instruction="s", prompt="p")


# -- Redaction and telemetry survive the other path ------------------------------


async def test_the_prompt_is_redacted_before_it_streams(monkeypatch):
    """Redaction lives at the transport so no call site can forget. A second
    method on that transport is exactly where that could quietly stop being
    true."""
    from app.services.ai.masking import redactor_for

    captured: list = []

    class _Model:
        def astream(self, messages):
            captured.extend(m.content for m in messages)

            async def _gen():
                yield _Chunk('{"ok": true}')

            return _gen()

    from app.services.ai import model_client as module

    monkeypatch.setattr(module.ModelClient, "_model_client", lambda self: _Model())
    client = ModelClient("k", "m", redactor=redactor_for("Priya Raman"))

    await client.stream_json(
        system_instruction="s", prompt="Priya Raman, priya@example.com"
    )

    sent = "\n".join(captured)
    assert "Priya" not in sent
    assert "priya@example.com" not in sent


async def test_time_to_first_token_is_recorded(monkeypatch):
    """The number streaming exists to improve, and the one a buffered call
    cannot produce."""
    client = _streaming_client(
        [_Chunk(""), _Chunk('{"a"'), _Chunk(": 1}")], monkeypatch
    )

    await client.stream_json(system_instruction="s", prompt="p")

    stats = call_metrics.snapshot()["by_operation"]["generate"]
    assert stats["streamed"] == 1
    assert stats["avg_first_token_ms"] is not None
    # One call, so the average and the max are the same measurement.
    assert stats["avg_first_token_ms"] == stats["max_first_token_ms"]


async def test_a_buffered_call_reports_no_first_token_time(monkeypatch):
    """Null rather than zero: the buffered path did not fail to be fast, it has
    no such measurement to make."""
    class _Model:
        async def ainvoke(self, messages):
            return _Chunk('{"ok": true}')

    from app.services.ai import model_client as module

    monkeypatch.setattr(module.ModelClient, "_model_client", lambda self: _Model())

    await ModelClient("k", "m").generate_json(system_instruction="s", prompt="p")

    stats = call_metrics.snapshot()["by_operation"]["generate"]
    assert stats["streamed"] == 0
    assert stats["avg_first_token_ms"] is None


async def test_usage_is_kept_from_whichever_chunk_carries_it(monkeypatch):
    """Providers disagree about which chunk holds usage -- first, last, or
    none -- so a later empty one must not erase what an earlier one reported."""
    client = _streaming_client(
        [
            _Chunk('{"a": 1}', usage={"input_tokens": 30, "output_tokens": 4}),
            _Chunk("", usage=None),
        ],
        monkeypatch,
    )

    await client.stream_json(system_instruction="s", prompt="p")

    snapshot = call_metrics.snapshot()
    assert snapshot["input_tokens"] == 30
    assert snapshot["output_tokens"] == 4


# -- Where it is wired ----------------------------------------------------------


async def test_follow_up_streams_only_when_asked(monkeypatch):
    """`initial_questions` is deliberately not wired: critique and refine run
    after generation, so a streamed set could be rewritten under the reader."""
    from app.services.ai.gemini import GeminiQuestionGenerator

    calls: list[str] = []

    class _Client:
        async def generate_json(self, *, system_instruction, prompt):
            calls.append("buffered")
            return {"ask_follow_up": False}

        async def stream_json(self, *, system_instruction, prompt, on_chunk=None):
            calls.append("streamed")
            return {"ask_follow_up": False}

    buffered = GeminiQuestionGenerator(_Client())  # type: ignore[arg-type]
    streaming = GeminiQuestionGenerator(_Client(), streaming=True)  # type: ignore[arg-type]

    await buffered.follow_up(question="q", answer="a", resume_text=None)
    await streaming.follow_up(question="q", answer="a", resume_text=None)

    assert calls == ["buffered", "streamed"]
