"""The embedding cache.

Its whole justification is arithmetic: 20 provider requests per day for the
account, one call per chunk to index a resume. So the tests that matter are
about *how many calls actually leave*, and about the failure mode where the
cache is unreachable and everything still works while quietly costing full
price.
"""

import pytest

from app.services.ai import retrieval_metrics
from app.services.ai.embedding import EmbeddingService
from app.services.ai.embedding_cache import EmbeddingCache
from app.services.ai.masking import redactor_for


@pytest.fixture(autouse=True)
def _clean_state():
    retrieval_metrics.reset()
    yield
    retrieval_metrics.reset()


class _FakeRedis:
    """An in-memory stand-in with the two methods the cache uses."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.values: dict[str, bytes] = {}
        self.error = error
        self.gets = 0
        self.sets = 0

    async def get(self, key: str) -> bytes | None:
        self.gets += 1
        if self.error:
            raise self.error
        return self.values.get(key)

    async def set(self, key: str, value: bytes, *, ex: int | None = None) -> None:
        self.sets += 1
        if self.error:
            raise self.error
        self.values[key] = value


def _cache(redis, model: str = "models/test-embedding") -> EmbeddingCache:
    return EmbeddingCache(redis, model=model, ttl_seconds=60)


# -- Round trip ----------------------------------------------------------------


async def test_a_vector_survives_the_round_trip():
    redis = _FakeRedis()
    cache = _cache(redis)
    vector = [0.125, -0.5, 0.75]

    await cache.set("some text", vector)

    assert await cache.get("some text") == pytest.approx(vector)


async def test_a_miss_is_none_and_is_counted():
    assert await _cache(_FakeRedis()).get("never seen") is None
    assert retrieval_metrics.snapshot()["cache_misses"] == 1


async def test_the_model_is_part_of_the_key():
    """Vectors from different models are not comparable -- mixing them in one
    index produces distances that mean nothing. Keying on the model is what
    makes changing GEMINI_EMBEDDING_MODEL safe: old entries are never read."""
    redis = _FakeRedis()
    await _cache(redis, model="model-a").set("text", [1.0])

    assert await _cache(redis, model="model-b").get("text") is None


async def test_the_key_does_not_contain_the_text():
    """The key is a digest. Redis holds no readable resume content."""
    key = _cache(_FakeRedis()).key("Ada Lovelace, ada@example.com")

    assert "Ada" not in key
    assert "example.com" not in key


# -- Failure is a miss, and is counted as neither ------------------------------


async def test_an_unreachable_cache_reads_as_a_miss_rather_than_raising():
    cache = _cache(_FakeRedis(error=ConnectionError("redis down")))

    assert await cache.get("text") is None


async def test_an_unreachable_cache_does_not_break_writes():
    cache = _cache(_FakeRedis(error=ConnectionError("redis down")))

    await cache.set("text", [1.0, 2.0])  # must not raise


async def test_errors_are_counted_apart_from_misses():
    """An unreachable cache behaves exactly like a cold one -- the request
    succeeds, at full provider cost. Counting the two as one number is how you
    keep paying while believing the cache works."""
    cache = _cache(_FakeRedis(error=ConnectionError("redis down")))

    await cache.get("text")
    await cache.set("text", [1.0])

    state = retrieval_metrics.snapshot()
    assert state["cache_errors"] == 2
    assert state["cache_misses"] == 0


async def test_a_corrupt_entry_is_discarded_not_raised():
    """Something else wrote under our key, or a value was truncated."""
    redis = _FakeRedis()
    cache = _cache(redis)
    redis.values[cache.key("text")] = b"\x00\x01\x02"  # not a whole float32

    assert await cache.get("text") is None
    assert retrieval_metrics.snapshot()["cache_errors"] == 1


async def test_an_empty_vector_is_not_stored():
    """embed_batch represents a per-chunk failure as an empty list; caching one
    would make the failure permanent for the lifetime of the entry."""
    redis = _FakeRedis()

    await _cache(redis).set("text", [])

    assert redis.sets == 0


# -- What the caller actually gains --------------------------------------------


class _CountingEmbeddings(EmbeddingService):
    """A real EmbeddingService with the HTTP call replaced by a counter."""

    def __init__(self, cache=None) -> None:
        super().__init__("test-key", model="models/test-embedding", cache=cache)
        self.calls: list[str] = []

    async def _embed_uncached(self, redacted: str) -> list[float]:
        self.calls.append(redacted)
        return [float(len(redacted)), 0.5]


async def test_re_embedding_the_same_text_costs_one_provider_call():
    """The point of the whole part: re-indexing an unchanged resume is free."""
    service = _CountingEmbeddings(cache=_cache(_FakeRedis()))

    first = await service.embed_text("Built an event pipeline on Kafka.")
    second = await service.embed_text("Built an event pipeline on Kafka.")

    assert first == pytest.approx(second)
    assert len(service.calls) == 1


async def test_a_batch_only_pays_for_the_chunks_it_has_not_seen():
    cache = _cache(_FakeRedis())
    service = _CountingEmbeddings(cache=cache)
    chunks = ["EXPERIENCE\nKafka", "EDUCATION\nPune", "SKILLS\nGo"]

    await service.embed_batch(chunks)
    await service.embed_batch(chunks + ["PROJECTS\nLedgerkit"])

    # Three the first time, one new one the second.
    assert len(service.calls) == 4


async def test_the_cache_is_keyed_on_the_redacted_text():
    """Redaction happens at the provider boundary, so hashing before it would
    put a fingerprint of the candidate's name in Redis -- and would miss for
    two resumes differing only in the identifiers that get redacted away."""
    redis = _FakeRedis()
    service = _CountingEmbeddings(cache=_cache(redis))

    await service.embed_text("Ada Lovelace built it", redactor=redactor_for("Ada Lovelace"))
    await service.embed_text("Grace Hopper built it", redactor=redactor_for("Grace Hopper"))

    # Both redact to "[REDACTED_NAME] built it", so the second is a hit.
    assert len(service.calls) == 1
    assert retrieval_metrics.snapshot()["cache_hits"] == 1


async def test_without_a_cache_every_call_reaches_the_provider():
    """No Redis is the intended configuration for local development, not a
    fault -- it must simply be slower and dearer, never wrong."""
    service = _CountingEmbeddings(cache=None)

    await service.embed_text("text")
    await service.embed_text("text")

    assert len(service.calls) == 2


async def test_an_unreachable_cache_still_returns_correct_embeddings():
    service = _CountingEmbeddings(cache=_cache(_FakeRedis(error=ConnectionError("down"))))

    first = await service.embed_text("text")
    second = await service.embed_text("text")

    assert first == second == pytest.approx([4.0, 0.5])
    # Full price, twice -- correct, and visible in cache_errors.
    assert len(service.calls) == 2
    assert retrieval_metrics.snapshot()["cache_errors"] == 4
