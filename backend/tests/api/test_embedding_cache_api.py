"""The embedding cache against a real Redis.

tests/test_embedding_cache.py proves the branching with a fake. What a fake
cannot prove is that a 3072-dimension vector survives the packing, the wire and
the round trip intact enough for cosine similarity, that the TTL is actually
applied, and that the value stored is bytes rather than a repr of a Python list.
"""

import math

import pytest

from app.services.ai import retrieval_metrics
from app.services.ai.embedding_cache import EmbeddingCache

# The real embedding width for models/gemini-embedding-001. Worth using the
# true size: the packing is what makes an entry 12 KiB instead of 60 KiB.
_DIMENSIONS = 3072


@pytest.fixture(autouse=True)
def _clean_metrics():
    retrieval_metrics.reset()
    yield
    retrieval_metrics.reset()


@pytest.fixture
def cache(redis_pool) -> EmbeddingCache:
    return EmbeddingCache(redis_pool, model="models/test-embedding", ttl_seconds=60)


def _vector() -> list[float]:
    """A plausible unit-norm embedding."""
    raw = [math.sin(index * 0.017) for index in range(_DIMENSIONS)]
    norm = math.sqrt(sum(value * value for value in raw))
    return [value / norm for value in raw]


async def test_a_full_size_vector_survives_the_round_trip(cache):
    vector = _vector()

    await cache.set("a chunk of resume text", vector)
    restored = await cache.get("a chunk of resume text")

    assert restored is not None
    assert len(restored) == _DIMENSIONS
    # float32, not float64: the loss is far below anything cosine similarity
    # can distinguish, which is the trade the packing makes.
    assert restored == pytest.approx(vector, abs=1e-6)


async def test_the_packing_preserves_cosine_similarity(cache):
    """The only property that actually matters downstream."""
    vector = _vector()
    await cache.set("text", vector)

    restored = await cache.get("text")

    assert restored is not None
    similarity = sum(a * b for a, b in zip(vector, restored))
    assert similarity == pytest.approx(1.0, abs=1e-6)


async def test_an_entry_is_packed_bytes_not_a_python_repr(cache, redis_pool):
    """A list of 3072 decimals is roughly 60 KiB of text; packed float32 is 12
    KiB. Storing the repr would work and quintuple the memory."""
    vector = _vector()
    await cache.set("text", vector)

    raw = await redis_pool.get(cache.key("text"))

    assert raw is not None
    assert len(raw) == _DIMENSIONS * 4
    assert not raw.startswith(b"[")


async def test_an_entry_expires(cache, redis_pool):
    """The TTL reclaims space rather than guarding correctness -- an embedding
    is a pure function of (model, text) and never goes stale -- but an entry
    with no expiry at all would grow the keyspace for ever."""
    await cache.set("text", _vector())

    ttl = await redis_pool.ttl(cache.key("text"))

    assert 0 < ttl <= 60


async def test_entries_stay_out_of_the_other_tenants_namespaces(cache, redis_pool):
    """One Redis holds the queue, the rate-limit counters and this."""
    await cache.set("text", _vector())

    keys = [key.decode() for key in await redis_pool.keys("*")]

    assert all(key.startswith("embed:") for key in keys)
    assert not any(key.startswith(("arq:", "ratelimit:")) for key in keys)


async def test_a_hit_and_a_miss_are_counted(cache):
    await cache.get("cold")
    await cache.set("warm", _vector())
    await cache.get("warm")

    state = retrieval_metrics.snapshot()
    assert (state["cache_misses"], state["cache_hits"]) == (1, 1)
    assert state["cache_errors"] == 0
