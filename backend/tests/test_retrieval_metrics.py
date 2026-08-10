"""Retrieval telemetry.

The point of this module is that retrieval's failure modes stop being silent,
so the tests are about what each mode records -- particularly the two that
produce a working interview with an unpersonalised prompt, which is what made
them invisible in the first place.
"""

import logging
import uuid

import pytest

from app.services.ai import retrieval_metrics
from app.services.ai.gemini import GeminiQuestionGenerator
from app.services.ai.rag import RAGService
from app.services.ai.vector_store import RetrievalResult


@pytest.fixture(autouse=True)
def _clean_state():
    retrieval_metrics.reset()
    yield
    retrieval_metrics.reset()


class _FakeEmbeddings:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def embed_text(self, text, *, redactor=None):
        if self.error:
            raise self.error
        return [0.1, 0.2, 0.3]

    async def embed_batch(self, texts, *, redactor=None):
        if self.error:
            raise self.error
        return [[0.1, 0.2, 0.3] for _ in texts]


class _FakeStore:
    def __init__(self, result: RetrievalResult | None = None) -> None:
        self.result = result or RetrievalResult([], [], [])
        self.added: list[tuple] = []

    async def retrieve_relevant(self, embedding, resume_id, top_k=5):
        return self.result

    async def add_resume(self, resume_id, user_id, chunks, embeddings):
        self.added.append((resume_id, chunks, embeddings))


def _rag(store=None, embeddings=None) -> RAGService:
    return RAGService(embeddings or _FakeEmbeddings(), store or _FakeStore())


# -- Retrieval outcomes --------------------------------------------------------


async def test_a_hit_records_the_chunk_count_and_the_best_distance():
    """The distance is the part worth keeping: five chunks at distance 1.9 are
    five irrelevant chunks, and counting them as a hit says otherwise."""
    store = _FakeStore(RetrievalResult(["a", "b"], [0.42, 0.77]))

    await _rag(store).retrieve_context(uuid.uuid4(), "kafka")

    state = retrieval_metrics.snapshot()
    assert (state["retrievals"], state["hits"]) == (1, 1)
    assert state["last_best_distance"] == 0.42
    assert retrieval_metrics.recent()[-1]["chunks"] == 2


async def test_an_empty_index_is_recorded_separately_from_a_hit():
    """Distinct because the fixes differ: an empty index means the resume was
    never indexed, a failure means retrieval is broken."""
    await _rag().retrieve_context(uuid.uuid4(), "kafka")

    state = retrieval_metrics.snapshot()
    assert (state["empty"], state["hits"], state["failed"]) == (1, 0, 0)


async def test_a_failure_is_recorded_with_the_error_and_still_raises():
    embeddings = _FakeEmbeddings(error=RuntimeError("provider down"))

    with pytest.raises(RuntimeError):
        await _rag(embeddings=embeddings).retrieve_context(uuid.uuid4(), "kafka")

    state = retrieval_metrics.snapshot()
    assert state["failed"] == 1
    assert "provider down" in str(retrieval_metrics.recent()[-1]["error"])


async def test_every_retrieval_is_timed():
    await _rag(_FakeStore(RetrievalResult(["a"], [0.5]))).retrieve_context(
        uuid.uuid4(), "kafka"
    )

    state = retrieval_metrics.snapshot()
    assert state["avg_ms"] is not None and state["max_ms"] is not None


async def test_the_purpose_distinguishes_initial_questions_from_follow_ups():
    """They retrieve on very different queries -- a role description versus a
    candidate's answer -- so their hit rates have to be readable apart."""
    rag = _rag(_FakeStore(RetrievalResult(["a"], [0.5])))

    await rag.retrieve_context(uuid.uuid4(), "role", purpose="initial_questions")
    await rag.retrieve_context(uuid.uuid4(), "answer", purpose="follow_up")

    assert [t["purpose"] for t in retrieval_metrics.recent()] == [
        "initial_questions",
        "follow_up",
    ]


# -- Indexing ------------------------------------------------------------------


async def test_indexing_records_produced_versus_embedded():
    """The ratio is the signal. A resume that produced 30 chunks and embedded 4
    retrieves partial answers for as long as it stays in the index."""
    store = _FakeStore()
    rag = RAGService(_FakeEmbeddings(), store)

    await rag.index_resume(uuid.uuid4(), uuid.uuid4(), "para one\n\npara two")

    state = retrieval_metrics.snapshot()
    assert state["resumes_indexed"] == 1
    assert state["chunks_produced"] >= 1
    assert state["chunks_embedded"] == state["chunks_produced"]


async def test_indexing_is_recorded_even_when_it_fails_partway():
    """Recorded in a finally: a run that chunked and then failed to embed is
    exactly the run worth seeing, and it never reaches the success path."""
    rag = RAGService(_FakeEmbeddings(error=RuntimeError("quota")), _FakeStore())

    with pytest.raises(RuntimeError):
        await rag.index_resume(uuid.uuid4(), uuid.uuid4(), "para one\n\npara two")

    state = retrieval_metrics.snapshot()
    assert state["chunks_produced"] > 0
    assert state["chunks_embedded"] == 0


# -- Availability --------------------------------------------------------------


def test_availability_starts_unknown_and_records_a_reason_when_off():
    """`CHROMA_PATH` unwritable disables RAG silently, and the only symptom is
    that the questions feel generic. The reason has to outlive the log line."""
    assert retrieval_metrics.snapshot()["enabled"] is None

    retrieval_metrics.record_availability(enabled=False, reason="init_failed: OSError")

    state = retrieval_metrics.snapshot()
    assert state["enabled"] is False
    assert state["disabled_reason"] == "init_failed: OSError"


def test_enabling_clears_a_previous_reason():
    retrieval_metrics.record_availability(enabled=False, reason="no_api_key")
    retrieval_metrics.record_availability(enabled=True)

    state = retrieval_metrics.snapshot()
    assert state["enabled"] is True
    assert state["disabled_reason"] is None


# -- The generator's view ------------------------------------------------------


class _SilentClient:
    async def generate_json(self, *, system_instruction, prompt):
        return {"questions": []}


async def test_the_generator_counts_every_route_to_a_truncated_prompt():
    """Retrieval that fails, retrieval that finds nothing, and retrieval that is
    never reached all produce the same de-personalised prompt. Only the
    generator sees all three, which is why it does the counting."""
    generator = GeminiQuestionGenerator(_SilentClient(), rag_service=None)

    fragment, used_rag = await generator._resume_context(
        resume_text="a resume", resume_id=uuid.uuid4(), query="q"
    )

    assert used_rag is False
    assert "a resume" in fragment
    state = retrieval_metrics.snapshot()
    assert state["full_text_fallbacks"] == 1
    # No retrieval was attempted, so the retrieval counters stay at zero -- the
    # pair of numbers is what separates "not reached" from "reached and empty".
    assert state["retrievals"] == 0


async def test_a_successful_retrieval_is_not_counted_as_a_fallback():
    generator = GeminiQuestionGenerator(
        _SilentClient(), rag_service=_rag(_FakeStore(RetrievalResult(["chunk"], [0.3])))
    )

    _, used_rag = await generator._resume_context(
        resume_text="a resume", resume_id=uuid.uuid4(), query="q"
    )

    assert used_rag is True
    assert retrieval_metrics.snapshot()["full_text_fallbacks"] == 0


async def test_a_session_with_no_resume_records_nothing():
    """Nothing has degraded: there is no resume to retrieve from."""
    generator = GeminiQuestionGenerator(_SilentClient(), rag_service=None)

    fragment, used_rag = await generator._resume_context(
        resume_text=None, resume_id=None, query="q"
    )

    assert (fragment, used_rag) == ("", False)
    assert retrieval_metrics.snapshot()["full_text_fallbacks"] == 0


# -- Structured logging --------------------------------------------------------


async def test_the_trace_is_logged_as_fields_not_prose(caplog):
    """"How often does retrieval return nothing" should be a query over logs,
    not an afternoon of grepping sentences."""
    with caplog.at_level(logging.INFO, logger="app.services.ai.retrieval_metrics"):
        await _rag(_FakeStore(RetrievalResult(["a"], [0.25]))).retrieve_context(
            uuid.uuid4(), "kafka"
        )

    record = next(r for r in caplog.records if r.message == "rag.retrieval")
    assert record.rag["outcome"] == "hit"
    assert record.rag["best_distance"] == 0.25
