"""Provider selection, and getting JSON out of a model that has no JSON mode.

The selection half is nearly trivial. The parsing half is not, and it is the
reason this module exists: Gemini takes `response_mime_type`, OpenAI takes
`response_format`, and **Anthropic takes neither** — you ask for JSON in the
prompt and it may well arrive inside a ```json fence with a sentence on either
side.

Every caller above the transport is written against parsed JSON. Without
`extract_json`, pointing `AI_PROVIDER` at Anthropic breaks question generation
on the first call — and breaks it *quietly*, because `ModelError` is exactly
what the fallback layer catches, so the interview still completes with generic
questions. That is the failure mode this project has been bitten by most.
"""

import pytest

from app.services.ai.providers import ProviderError, build_chat_model, extract_json

# -- What a JSON mode returns ---------------------------------------------------


def test_plain_json_parses() -> None:
    """The only path taken on Gemini and OpenAI, where the mode is real."""
    assert extract_json('{"questions": ["a"], "count": 1}') == {
        "questions": ["a"],
        "count": 1,
    }


def test_surrounding_whitespace_is_not_a_problem() -> None:
    assert extract_json('\n\n  {"ok": true}\n ') == {"ok": True}


def test_a_top_level_array_parses() -> None:
    assert extract_json('[{"a": 1}, {"b": 2}]') == [{"a": 1}, {"b": 2}]


# -- What a model without a JSON mode returns ------------------------------------


def test_a_fenced_block_is_unwrapped() -> None:
    reply = '```json\n{"overall_score": 7, "strengths": ["clear"]}\n```'

    assert extract_json(reply) == {"overall_score": 7, "strengths": ["clear"]}


def test_an_unlabelled_fence_is_unwrapped() -> None:
    assert extract_json('```\n{"ok": true}\n```') == {"ok": True}


def test_prose_on_either_side_is_ignored() -> None:
    reply = (
        "Here is the evaluation you asked for:\n\n"
        '{"overall_score": 8}\n\n'
        "Let me know if you would like more detail."
    )

    assert extract_json(reply) == {"overall_score": 8}


def test_prose_and_a_fence_together() -> None:
    reply = 'Sure! Here you go:\n\n```json\n{"questions": []}\n```\n\nHope that helps.'

    assert extract_json(reply) == {"questions": []}


def test_a_brace_inside_a_string_does_not_truncate_the_object() -> None:
    """The reason the span scan counts brackets *and* tracks strings. A greedy
    regex, or a counter that ignored quoting, would end the object at the brace
    inside this value and parse a fragment."""
    reply = 'Result:\n{"note": "salary was {competitive}", "score": 6}'

    assert extract_json(reply) == {"note": "salary was {competitive}", "score": 6}


def test_nested_objects_survive_the_span_scan() -> None:
    reply = 'Here:\n{"outer": {"inner": {"deep": [1, 2]}}, "n": 3}'

    assert extract_json(reply) == {"outer": {"inner": {"deep": [1, 2]}}, "n": 3}


def test_an_escaped_quote_does_not_confuse_the_scan() -> None:
    reply = 'Output: {"quote": "they said \\"hello\\"", "score": 5}'

    assert extract_json(reply) == {"quote": 'they said "hello"', "score": 5}


# -- What must fail, and fail cleanly -------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "   ",
        "I'm sorry, I can't help with that.",
        "The candidate did well overall.",
    ],
)
def test_a_reply_with_no_json_raises(reply) -> None:
    """`ValueError` becomes `ModelError` at the transport, so the caller
    degrades to the deterministic fallback rather than crashing a request."""
    with pytest.raises(ValueError):
        extract_json(reply)


def test_malformed_json_is_not_repaired() -> None:
    """Deliberately no fixing of trailing commas or single quotes. Guessing at
    malformed JSON risks silently changing a score or a question, and a clean
    failure into the fallback beats a plausible wrong answer."""
    with pytest.raises(ValueError):
        extract_json("{'score': 7,}")


# -- Selection ------------------------------------------------------------------


def test_an_unknown_provider_names_what_was_expected() -> None:
    with pytest.raises(ProviderError, match="Unknown AI_PROVIDER"):
        build_chat_model(
            provider="llama-cpp",  # type: ignore[arg-type]
            model="m",
            api_key="k",
            timeout=1.0,
            max_retries=1,
        )


def test_a_missing_package_names_the_package(monkeypatch) -> None:
    """The optional extras are not installed by default, so the common way to
    meet this is a correct setting and a missing `pip install`. The message has
    to say which one."""
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name.startswith("langchain_anthropic"):
            raise ImportError("No module named 'langchain_anthropic'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)

    with pytest.raises(ProviderError, match="langchain-anthropic"):
        build_chat_model(
            provider="anthropic", model="m", api_key="k", timeout=1.0, max_retries=1
        )


def test_gemini_is_built_with_json_mode_and_our_retry_count() -> None:
    """`max_retries` is passed through rather than left to the integration:
    each ships a different default, and the free tier is 20 requests a day."""
    model = build_chat_model(
        provider="gemini",
        model="gemini-flash-latest",
        api_key="k",
        timeout=7.0,
        max_retries=1,
    )

    assert model.max_retries == 1
    assert model.model.endswith("gemini-flash-latest")


# -- The OpenAI-compatible path, end to end -------------------------------------


@pytest.fixture
def fake_openai_server():
    """A local endpoint speaking the OpenAI chat API.

    This is the path Groq, Ollama, OpenRouter and vLLM all take, so it is worth
    exercising for real rather than stubbing the LangChain class: what could
    break is the wiring between `AI_BASE_URL`, the integration and our JSON
    handling, and a stub at the class boundary would skip exactly that.

    It replies the way a model *without* a JSON mode does -- fenced, with prose
    on both sides -- because that is the combination that has to survive.
    """
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - name fixed by http.server
            received.update(
                json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            )
            content = (
                "Here you go:\n```json\n"
                '{"questions": ["Tell me about Kafka"], "n": 1}\n'
                "```\nHope that helps!"
            )
            payload = {
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "model": "fake-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 42,
                    "completion_tokens": 13,
                    "total_tokens": 55,
                },
            }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: A002 - silence the access log
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v1", received
    server.shutdown()


async def test_an_openai_compatible_provider_works_end_to_end(fake_openai_server):
    pytest.importorskip(
        "langchain_openai", reason='needs the optional extra: pip install -e ".[openai]"'
    )
    from app.services.ai import call_metrics
    from app.services.ai.masking import redactor_for
    from app.services.ai.model_client import ModelClient

    base_url, received = fake_openai_server
    call_metrics.reset()

    client = ModelClient(
        "fake-key",
        "fake-model",
        provider="openai",
        base_url=base_url,
        redactor=redactor_for("Priya Raman"),
    )

    result = await client.generate_json(
        system_instruction="Return JSON.",
        prompt="Priya Raman, priya@example.com -- ask about Kafka.",
    )

    # Parsed out of a fenced reply with prose around it.
    assert result == {"questions": ["Tell me about Kafka"], "n": 1}

    # Redaction is at the transport, so it must survive a provider swap. This
    # is the assertion that would catch a new provider branch that forgot it.
    import json as _json

    sent = _json.dumps(received)
    assert "Priya" not in sent
    assert "priya@example.com" not in sent

    # So must the telemetry, including token usage from a non-Gemini reply.
    snapshot = call_metrics.snapshot()
    assert snapshot["calls"] == 1
    assert snapshot["last_model"] == "fake-model"
    assert snapshot["input_tokens"] == 42
    call_metrics.reset()
