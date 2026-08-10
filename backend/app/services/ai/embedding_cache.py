"""Cache for embedding vectors.

An embedding is a pure function of (model, text), so it is cacheable in the
strongest sense: the same inputs cannot produce a different answer, and a hit
is not an approximation of the provider's reply, it *is* the provider's reply.

The reason it matters here is arithmetic. The free tier allows **20 requests
per day for the whole account**, and indexing one resume costs one call per
chunk -- nine for the benchmark fixture, more for a real CV. So two uploads
exhaust the day for every user of the deployment, and `reprocess()`, which
exists precisely to rebuild an index, was unaffordable in practice. With this,
re-indexing unchanged text costs nothing.

**The key is the hash of the redacted text**, which is why the cache lives
inside `EmbeddingService` rather than wrapping it. Redaction happens at the
provider boundary, so hashing before it would put a fingerprint of the
candidate's name and email in Redis and would also miss: two texts differing
only in redacted identifiers embed identically and should share an entry.

Keys are not scoped per user, deliberately. The value is a pure function of the
text, so a hit tells the requester nothing they did not already supply -- they
had to present the text to look it up. Scoping by user would only mean the
common case, re-indexing your own resume, still worked while cross-user
deduplication silently did not.

Every failure here is swallowed. A cache that cannot be reached must slow the
system down, not break it, so a miss and an outage take the same path -- but
they are counted separately, because an outage that reads as a miss is how you
end up paying full price and thinking the cache is working.
"""

from __future__ import annotations

import hashlib
import logging
from array import array
from typing import TYPE_CHECKING

from app.services.ai import retrieval_metrics

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

_KEY_PREFIX = "embed:"

# Vectors are stored as packed float32 rather than JSON: 3072 dimensions is
# 12 KiB packed against roughly 60 KiB of decimal text, and the provider's
# values do not carry more precision than a float32 holds. The loss is far
# below anything cosine similarity can distinguish.
_PACK_FORMAT = "f"


class EmbeddingCache:
    """Redis-backed store of embedding vectors, keyed by content."""

    def __init__(self, redis: "Redis", *, model: str, ttl_seconds: int) -> None:
        self._redis = redis
        self._model = model
        self._ttl = ttl_seconds

    def key(self, redacted_text: str) -> str:
        """Cache key for one already-redacted string.

        The model is part of the key because vectors from different models are
        not comparable -- mixing them in one index produces distances that mean
        nothing. This is what makes changing GEMINI_EMBEDDING_MODEL safe: the
        old entries are simply never read again.
        """
        digest = hashlib.sha256(redacted_text.encode("utf-8")).hexdigest()
        return f"{_KEY_PREFIX}{self._model}:{digest}"

    async def get(self, redacted_text: str) -> list[float] | None:
        """The cached vector, or None on a miss *or* any failure."""
        try:
            packed = await self._redis.get(self.key(redacted_text))
        except Exception as exc:  # noqa: BLE001 - a cache must not break embedding
            retrieval_metrics.record_cache(outcome="error")
            logger.warning("Embedding cache read failed: %s", exc)
            return None

        if packed is None:
            retrieval_metrics.record_cache(outcome="miss")
            return None

        try:
            vector = array(_PACK_FORMAT)
            vector.frombytes(packed)
        except Exception as exc:  # noqa: BLE001 - corrupt entry, not a crash
            # A truncated or foreign value under our key. Treat it as absent;
            # the next write replaces it.
            retrieval_metrics.record_cache(outcome="error")
            logger.warning("Discarding an unreadable embedding cache entry: %s", exc)
            return None

        retrieval_metrics.record_cache(outcome="hit")
        return list(vector)

    async def set(self, redacted_text: str, vector: list[float]) -> None:
        """Store a vector. Never raises."""
        if not vector:
            return
        try:
            await self._redis.set(
                self.key(redacted_text),
                array(_PACK_FORMAT, vector).tobytes(),
                ex=self._ttl,
            )
        except Exception as exc:  # noqa: BLE001 - a cache must not break embedding
            retrieval_metrics.record_cache(outcome="error")
            logger.warning("Embedding cache write failed: %s", exc)
