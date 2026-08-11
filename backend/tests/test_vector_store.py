"""The vector store's contract with everything above it.

Real Chroma, in memory, no provider and no database. These cover the seam
`RAGService`, `HybridRetriever` and the benchmark are written against, and in
particular the parts of it that fail *quietly*:

- `retrieve_relevant` returns cosine **distances**, ascending. The method it
  calls is named `..._with_relevance_scores`, and a reading in the other
  direction turns `RAG_MAX_DISTANCE` into a filter that keeps exactly what it
  exists to drop -- with no exception anywhere.
- `delete_resume` catches everything and logs a warning, so a delete that
  matches nothing looks identical to one that worked.

Every test takes its own collection name. An in-memory chromadb client is not
private -- one system is shared per process -- so a fixed name lets one test's
vector dimensions collide with another's, which only shows up in a full run.
"""

from __future__ import annotations

import uuid

import pytest

from app.services.ai.vector_store import (
    ChromaVectorStore,
    VectorStoreError,
    _PrecomputedEmbeddings,
)


@pytest.fixture
def store() -> ChromaVectorStore:
    return ChromaVectorStore(None, collection_name=f"unit-{uuid.uuid4()}")


# Two orthogonal vectors, so the expected distances are exact rather than
# approximate: identical is 0.0, orthogonal is 1.0.
_ALPHA = [1.0, 0.0]
_GAMMA = [0.0, 1.0]


# -- Round trip -----------------------------------------------------------------


async def test_chunks_come_back_ranked_by_ascending_distance(store) -> None:
    resume_id, user_id = uuid.uuid4(), uuid.uuid4()
    await store.add_resume(
        resume_id, user_id, ["alpha beta", "gamma delta"], [_ALPHA, _GAMMA]
    )

    result = await store.retrieve_relevant(_ALPHA, resume_id, top_k=5)

    assert result.documents == ["alpha beta", "gamma delta"]
    # The direction the cutoff depends on: nearest first, 0.0 for the identical
    # vector and 1.0 for the orthogonal one. A similarity would be the reverse.
    assert result.distances[0] == pytest.approx(0.0)
    assert result.distances[1] == pytest.approx(1.0)
    assert result.distances == sorted(result.distances)


async def test_a_query_only_sees_its_own_resume(store) -> None:
    """The metadata filter is the only thing separating two users' resumes in
    one collection."""
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    await store.add_resume(mine, uuid.uuid4(), ["my resume"], [_ALPHA])
    await store.add_resume(theirs, uuid.uuid4(), ["their resume"], [_ALPHA])

    result = await store.retrieve_relevant(_ALPHA, mine, top_k=10)

    assert result.documents == ["my resume"]
    assert all(meta["resume_id"] == str(mine) for meta in result.metadatas)


async def test_an_unindexed_resume_retrieves_nothing_rather_than_raising(store) -> None:
    result = await store.retrieve_relevant(_ALPHA, uuid.uuid4(), top_k=5)

    assert result.documents == []
    assert result.distances == []


async def test_metadata_carries_the_ordinal(store) -> None:
    """`chunk_index` is what ties a vector back to its row in `resume_chunks`."""
    resume_id = uuid.uuid4()
    await store.add_resume(resume_id, uuid.uuid4(), ["first", "second"], [_ALPHA, _GAMMA])

    result = await store.retrieve_relevant(_ALPHA, resume_id, top_k=5)

    assert [meta["chunk_index"] for meta in result.metadatas] == [0, 1]


# -- Indexing -------------------------------------------------------------------


async def test_re_indexing_replaces_a_chunk_in_place(store) -> None:
    """Chunk ids are derived from the ordinal, so a second run writes over the
    first rather than failing on a duplicate id."""
    resume_id, user_id = uuid.uuid4(), uuid.uuid4()
    await store.add_resume(resume_id, user_id, ["original"], [_ALPHA])

    await store.add_resume(resume_id, user_id, ["rewritten"], [_ALPHA])

    result = await store.retrieve_relevant(_ALPHA, resume_id, top_k=5)
    assert result.documents == ["rewritten"]


async def test_mismatched_chunks_and_embeddings_are_refused(store) -> None:
    with pytest.raises(VectorStoreError, match="same length"):
        await store.add_resume(uuid.uuid4(), uuid.uuid4(), ["a", "b"], [_ALPHA])


async def test_indexing_nothing_is_not_an_error(store) -> None:
    await store.add_resume(uuid.uuid4(), uuid.uuid4(), [], [])


# -- Deleting -------------------------------------------------------------------


async def test_deleting_removes_one_resume_and_leaves_the_rest(store) -> None:
    """`delete_resume` swallows its exceptions by design, so nothing but a
    read-back proves the filter matched anything at all."""
    doomed, kept = uuid.uuid4(), uuid.uuid4()
    user_id = uuid.uuid4()
    await store.add_resume(doomed, user_id, ["going", "also going"], [_ALPHA, _GAMMA])
    await store.add_resume(kept, user_id, ["staying"], [_ALPHA])

    await store.delete_resume(doomed)

    assert (await store.retrieve_relevant(_ALPHA, doomed, top_k=10)).documents == []
    assert (await store.retrieve_relevant(_ALPHA, kept, top_k=10)).documents == [
        "staying"
    ]


# -- The embedding courier ------------------------------------------------------


def test_the_courier_returns_the_vectors_it_was_given() -> None:
    """Reordered by text, because `add_texts` regroups its input before asking."""
    courier = _PrecomputedEmbeddings({"one": _ALPHA, "two": _GAMMA})

    assert courier.embed_documents(["two", "one"]) == [_GAMMA, _ALPHA]


def test_the_courier_refuses_to_embed_a_query() -> None:
    """Embedding here would be a provider call outside `EmbeddingService` --
    outside redaction, outside the cache, against a 20/day quota. It must fail
    loudly rather than quietly work."""
    with pytest.raises(VectorStoreError, match="does not embed queries"):
        _PrecomputedEmbeddings({}).embed_query("anything")


def test_the_courier_refuses_a_text_it_was_not_given() -> None:
    with pytest.raises(VectorStoreError, match="not given"):
        _PrecomputedEmbeddings({"known": _ALPHA}).embed_documents(["unknown"])
