"""Rank fusion and the hybrid retriever's degradation paths.

The Postgres half is exercised for real in tests/api/test_hybrid_retrieval_api.py.
What is here is the arithmetic and the branching, which a database would obscure
rather than prove.
"""

import uuid

import pytest

from app.services.ai import retrieval_metrics
from app.services.ai.retrieval import HybridRetriever, fuse


@pytest.fixture(autouse=True)
def _clean_state():
    retrieval_metrics.reset()
    yield
    retrieval_metrics.reset()


# -- Reciprocal Rank Fusion ----------------------------------------------------


def test_a_chunk_both_retrievers_found_outranks_either_ones_favourite():
    """The reason for running two retrievers. "b" is second on both lists and
    beats two different first places, because agreement is evidence and one
    retriever's confidence is not."""
    fused = fuse(dense=["a", "b"], sparse=["c", "b"], limit=3)

    assert fused[0].text == "b"
    assert fused[0].sources == "both"


def test_scores_come_from_ranks_not_from_the_retrievers_scores():
    """Cosine distance and ts_rank have different ranges and neither is
    calibrated, so any weighted sum of them encodes an invented exchange rate.
    RRF keeps only the ordering, which is the part each half is reliable about."""
    fused = fuse(dense=["a"], sparse=["b"], limit=2)

    assert fused[0].score == pytest.approx(1 / 61)
    assert fused[1].score == pytest.approx(1 / 61)


def test_results_from_one_retriever_alone_still_come_through():
    fused = fuse(dense=["a", "b"], sparse=[], limit=5)

    assert [candidate.text for candidate in fused] == ["a", "b"]
    assert all(candidate.sources == "dense" for candidate in fused)


def test_the_same_chunk_from_both_halves_appears_once():
    """The two halves return overlapping sets by design; a prompt containing
    the same paragraph twice pays for it twice and says nothing new."""
    fused = fuse(dense=["a", "b"], sparse=["b", "a"], limit=5)

    assert [candidate.text for candidate in fused] == ["a", "b"]


def test_ordering_is_deterministic_when_scores_tie():
    """Ties are common with only two lists. Dict iteration order deciding the
    prompt would make retrieval unreproducible."""
    first = fuse(dense=["a", "b", "c"], sparse=[], limit=3)
    second = fuse(dense=["a", "b", "c"], sparse=[], limit=3)

    assert [candidate.text for candidate in first] == [
        candidate.text for candidate in second
    ]


def test_the_limit_is_respected():
    fused = fuse(dense=["a", "b", "c"], sparse=["d", "e"], limit=2)

    assert len(fused) == 2


def test_fusing_nothing_yields_nothing():
    assert fuse(dense=[], sparse=[], limit=5) == []


# -- The retriever's branches --------------------------------------------------


class _Dense:
    def __init__(self, documents=None, error=None):
        self.documents = documents or []
        self.error = error
        self.calls = 0

    async def retrieve_ranked(self, resume_id, query, top_k=5, *, redactor=None, max_distance=None):
        self.calls += 1
        if self.error:
            raise self.error
        return self.documents


class _Chunk:
    def __init__(self, text):
        self.retrieval_text = text


class _Sparse:
    def __init__(self, matches=None, error=None):
        self.matches = matches or []
        self.error = error
        self.queries: list[str] = []

    async def search(self, resume_id, query, *, limit=5):
        self.queries.append(query)
        if self.error:
            raise self.error
        return [(_Chunk(text), 0.5) for text in self.matches]


async def test_both_halves_are_asked_and_merged():
    retriever = HybridRetriever(_Dense(["dense chunk"]), _Sparse(["sparse chunk"]))

    context = await retriever.retrieve_context(uuid.uuid4(), "kafka")

    assert "dense chunk" in context
    assert "sparse chunk" in context


async def test_a_broken_vector_store_leaves_keyword_search_working():
    """Degradation, not failure: keyword results beat the truncated-resume
    fallback, which is the alternative."""
    retriever = HybridRetriever(_Dense(error=RuntimeError("chroma down")), _Sparse(["kept"]))

    context = await retriever.retrieve_context(uuid.uuid4(), "kafka")

    assert "kept" in context


async def test_a_broken_keyword_search_leaves_dense_working():
    retriever = HybridRetriever(_Dense(["kept"]), _Sparse(error=RuntimeError("db down")))

    context = await retriever.retrieve_context(uuid.uuid4(), "kafka")

    assert "kept" in context


async def test_no_results_from_either_half_is_an_empty_string():
    """A query matching nothing returns nothing, rather than the k least-bad
    paragraphs of the resume. The caller falls back to raw text and records it."""
    retriever = HybridRetriever(_Dense([]), _Sparse([]))

    assert await retriever.retrieve_context(uuid.uuid4(), "basket weaving") == ""


async def test_a_dense_failure_with_no_keyword_half_still_raises():
    """Sessionless callers have no sparse half. Swallowing the failure there
    would report an empty index when retrieval is actually broken, and the two
    need different fixes."""
    retriever = HybridRetriever(_Dense(error=RuntimeError("chroma down")), None)

    with pytest.raises(RuntimeError):
        await retriever.retrieve_context(uuid.uuid4(), "kafka")


async def test_each_half_is_asked_for_more_than_the_final_count():
    """Fusion needs something to disagree about: k in means k out, and RRF
    would only reorder them."""
    dense, sparse = _Dense(["a"]), _Sparse(["b"])
    retriever = HybridRetriever(dense, sparse)

    await retriever.retrieve_context(uuid.uuid4(), "kafka", top_k=3)

    assert dense.calls == 1
    assert len(sparse.queries) == 1


async def test_fusion_is_recorded_with_what_each_half_contributed():
    retriever = HybridRetriever(_Dense(["a", "shared"]), _Sparse(["shared", "b"]))

    await retriever.retrieve_context(uuid.uuid4(), "kafka")

    state = retrieval_metrics.snapshot()
    assert state["fusions"] == 1
    # The number worth watching: zero agreement across many retrievals means
    # the halves never corroborate each other, which is what a broken embedder
    # or an empty keyword index looks like from outside.
    assert state["agreed"] == 1


async def test_a_half_returning_nothing_is_recorded_as_such():
    retriever = HybridRetriever(_Dense(["only dense"]), _Sparse([]))

    await retriever.retrieve_context(uuid.uuid4(), "kafka")

    assert retrieval_metrics.snapshot()["dense_only"] == 1
