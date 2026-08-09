"""What actually leaves the process.

tests/test_masking.py checks the rules in isolation. These check the thing the
rules exist for: that the HTTP body sent to Google carries no identifier. They
assert on the captured request, not on a return value, because a redactor that
is constructed correctly and then not applied would pass every other test.
"""

import json
from typing import Any

import httpx
import pytest

from app.services.ai import embedding as embedding_module
from app.services.ai import gemini_client as gemini_module
from app.services.ai.embedding import EmbeddingService
from app.services.ai.gemini_client import GeminiClient
from app.services.ai.masking import redactor_for
from app.services.ai.rag import RAGService

RESUME = (
    "Ada Lovelace\n"
    "ada.lovelace@example.com | +1 (555) 123-4567\n"
    "linkedin.com/in/adalovelace\n"
    "Senior Engineer at Stripe, 2019-2023. Cut p99 latency from 1200ms to 300ms."
)

# Every identifier in RESUME, in the exact form it is written there.
IDENTIFIERS = [
    "Ada",
    "Lovelace",
    "ada.lovelace@example.com",
    "555",
    "123-4567",
    "linkedin.com/in/adalovelace",
]


class _CapturingTransport:
    """Stands in for httpx.AsyncClient and records what was posted."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> "_CapturingTransport":
        return self

    async def __aenter__(self) -> "_CapturingTransport":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(self, url: str, *, headers: Any = None, json: Any = None) -> Any:
        self.bodies.append(json)
        return httpx.Response(200, json=self._payload)

    @property
    def sent_text(self) -> str:
        """Everything posted, flattened, so a leak anywhere in the body fails."""
        return "\n".join(json_dumps(body) for body in self.bodies)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


@pytest.fixture
def gemini_transport(monkeypatch: pytest.MonkeyPatch) -> _CapturingTransport:
    transport = _CapturingTransport(
        {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]}
    )
    monkeypatch.setattr(gemini_module.httpx, "AsyncClient", transport)
    return transport


@pytest.fixture
def embedding_transport(monkeypatch: pytest.MonkeyPatch) -> _CapturingTransport:
    transport = _CapturingTransport({"embedding": {"values": [0.1, 0.2, 0.3]}})
    monkeypatch.setattr(embedding_module.httpx, "AsyncClient", transport)
    return transport


def assert_no_identifiers(sent: str) -> None:
    leaked = [value for value in IDENTIFIERS if value in sent]
    assert not leaked, f"sent to the provider in the clear: {leaked}"


# -- Prompts -------------------------------------------------------------------


async def test_prompt_reaches_the_provider_without_identifiers(gemini_transport) -> None:
    client = GeminiClient("k", "m", redactor=redactor_for("Ada Lovelace"))

    await client.generate_json(system_instruction="Be rigorous.", prompt=RESUME)

    assert_no_identifiers(gemini_transport.sent_text)


async def test_prompt_keeps_the_content_the_interview_is_built_from(
    gemini_transport,
) -> None:
    """Redaction that also removes the employer would empty the product."""
    client = GeminiClient("k", "m", redactor=redactor_for("Ada Lovelace"))

    await client.generate_json(system_instruction="Be rigorous.", prompt=RESUME)

    sent = gemini_transport.sent_text
    for kept in ["Senior Engineer", "Stripe", "2019-2023", "1200ms", "300ms"]:
        assert kept in sent, f"{kept} was redacted but carries interview signal"


async def test_a_client_given_no_redactor_still_redacts_patterns(
    gemini_transport,
) -> None:
    """The floor. Forgetting to plumb an identity must not disable redaction."""
    client = GeminiClient("k", "m")

    await client.generate_json(system_instruction="Be rigorous.", prompt=RESUME)

    sent = gemini_transport.sent_text
    assert "ada.lovelace@example.com" not in sent
    assert "linkedin.com/in/adalovelace" not in sent
    assert "123-4567" not in sent
    # The name is the one thing patterns cannot know, so it survives here.
    assert "Lovelace" in sent


async def test_system_instruction_is_sent_verbatim(gemini_transport) -> None:
    """It is a constant in this repository, never user data."""
    instruction = "Respond as JSON: {\"questions\": [{\"content\": str}]}."
    client = GeminiClient("k", "m", redactor=redactor_for("Ada Lovelace"))

    await client.generate_json(system_instruction=instruction, prompt="Hello")

    body = gemini_transport.bodies[0]
    assert body["system_instruction"]["parts"][0]["text"] == instruction


async def test_redaction_is_logged_as_a_tally_never_as_values(
    gemini_transport, caplog
) -> None:
    client = GeminiClient("k", "m", redactor=redactor_for("Ada Lovelace"))

    with caplog.at_level("INFO", logger="app.services.ai.gemini_client"):
        await client.generate_json(system_instruction="s", prompt=RESUME)

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "Redacted" in logged
    assert_no_identifiers(logged)


# -- Embeddings ----------------------------------------------------------------


async def test_embedding_request_carries_no_identifiers(embedding_transport) -> None:
    service = EmbeddingService("k")

    await service.embed_text(RESUME, redactor=redactor_for("Ada Lovelace"))

    assert_no_identifiers(embedding_transport.sent_text)


async def test_embedding_without_a_redactor_still_redacts_patterns(
    embedding_transport,
) -> None:
    service = EmbeddingService("k")

    await service.embed_text(RESUME)

    assert "ada.lovelace@example.com" not in embedding_transport.sent_text


async def test_every_text_in_a_batch_is_redacted(embedding_transport) -> None:
    service = EmbeddingService("k")

    await service.embed_batch(
        ["clean text", RESUME, "ada@example.com"],
        redactor=redactor_for("Ada Lovelace"),
    )

    sent = embedding_transport.sent_text
    assert len(embedding_transport.bodies) == 3
    assert_no_identifiers(sent)
    assert "ada@example.com" not in sent


# -- Indexing ------------------------------------------------------------------


class _RecordingVectorStore:
    def __init__(self) -> None:
        self.documents: list[str] = []

    async def add_resume(self, resume_id, user_id, chunks, embeddings) -> None:
        self.documents.extend(chunks)


async def test_indexing_redacts_what_is_sent_but_stores_the_original(
    embedding_transport,
) -> None:
    """Chroma is ours; Google is not.

    The vector store holds the resume in full for the same reason Postgres
    holds `parsed_text` in full -- it is the user's own data in our own
    storage. Only the copy crossing the network is redacted.
    """
    import uuid

    store = _RecordingVectorStore()
    rag = RAGService(EmbeddingService("k"), store)

    await rag.index_resume(
        uuid.uuid4(), uuid.uuid4(), RESUME, redactor=redactor_for("Ada Lovelace")
    )

    assert_no_identifiers(embedding_transport.sent_text)
    assert "ada.lovelace@example.com" in "\n".join(store.documents)


async def test_retrieval_redacts_the_query(embedding_transport) -> None:
    """A follow-up query is the candidate's own answer text."""
    import uuid

    class _Store:
        async def retrieve_relevant(self, embedding, resume_id, top_k=5):
            class _Result:
                documents: list[str] = []

            return _Result()

    rag = RAGService(EmbeddingService("k"), _Store())

    await rag.retrieve_context(
        uuid.uuid4(),
        "I can be reached at ada.lovelace@example.com",
        redactor=redactor_for("Ada Lovelace"),
    )

    assert_no_identifiers(embedding_transport.sent_text)
