"""Embedding service using Gemini API for text vectorization.

The second place text leaves for Google, and the one that is easy to forget:
indexing a resume ships the whole document a chunk at a time. Redaction happens
here for the same reason it happens in GeminiClient -- at the boundary, so no
call site can omit it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.services.ai import call_metrics
from app.services.ai.masking import Redactor, default_redactor
from app.services.ai.tracing import traced

if TYPE_CHECKING:
    from app.services.ai.embedding_cache import EmbeddingCache

logger = logging.getLogger(__name__)

class EmbeddingError(RuntimeError):
    """Raised when embedding generation fails."""


class EmbeddingService:
    """Generate embeddings for text using Google's Gemini embedding model."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "models/gemini-embedding-001",
        timeout: float = 30.0,
        cache: "EmbeddingCache | None" = None,
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._model = model
        # Consulted *after* redaction, which is why the cache is in here rather
        # than wrapped around this class: hashing the raw text would put a
        # fingerprint of the candidate's name and email in Redis, and would
        # miss for two resumes that differ only in redacted identifiers.
        self._cache = cache
        self._embeddings: Any = None

    async def embed_text(
        self, text: str, *, redactor: Redactor | None = None
    ) -> list[float]:
        """Generate embedding for a single text.

        Args:
            text: Text to embed. Redacted before it leaves the process.
            redactor: Identity-aware redactor. Omitting it falls back to the
                pattern-only default rather than to no redaction.

        Returns:
            Embedding vector as list[float].

        Raises:
            EmbeddingError: If embedding generation fails.
        """
        if not text or not text.strip():
            raise EmbeddingError("Cannot embed empty text.")

        # Documents and queries are both redacted, so they stay in the same
        # space and similarity is unaffected. The identifiers that disappear
        # carry no meaning a retrieval could use anyway.
        redacted = (redactor or default_redactor()).redact(text)

        if self._cache is not None:
            cached = await self._cache.get(redacted)
            if cached is not None:
                return cached

        embedding = await self._embed_uncached(redacted)

        if self._cache is not None:
            await self._cache.set(redacted, embedding)
        return embedding

    def _model_client(self) -> Any:
        """Build the embedding model once, on first use.

        Lazy and cached for the same reason as the chat client: the
        integration pulls in google-genai and its auth stack, and this class is
        only constructed when an API key exists.
        """
        if self._embeddings is None:
            from langchain_google_genai import GoogleGenerativeAIEmbeddings
            from pydantic import SecretStr

            self._embeddings = GoogleGenerativeAIEmbeddings(
                # SecretStr, so the key cannot be printed by a repr of this
                # object -- the same concern that moved it out of the query
                # string when it was found in the logs.
                model=self._model,
                api_key=SecretStr(self._api_key),
            )
        return self._embeddings

    @traced("gemini.embed", run_type="llm")
    async def _embed_uncached(self, redacted: str) -> list[float]:
        """The provider call itself. Takes already-redacted text.

        The transport is LangChain's integration; the redaction, the cache and
        the EmbeddingError contract around it are unchanged, so callers and the
        batch semantics below neither changed nor noticed.
        """
        try:
            # No token counts: Google's embedding endpoint returns no usage, so
            # these calls contribute latency and a request count and nothing to
            # the token figures. The request count is the one that binds anyway
            # -- twenty a day for the whole account.
            with call_metrics.measure("embed", self._model):
                embedding = await self._model_client().aembed_query(redacted)
        except Exception as exc:  # noqa: BLE001 - the integration raises its own
            # Everything above is written against EmbeddingError, and
            # embed_batch keys on it to represent a per-chunk failure.
            raise EmbeddingError(f"Embedding request failed: {exc}") from exc

        if not isinstance(embedding, list) or not embedding:
            raise EmbeddingError("Embedding API returned no vector.")
        return embedding

    async def embed_batch(
        self, texts: list[str], *, redactor: Redactor | None = None
    ) -> list[list[float]]:
        """Generate embeddings for multiple texts.

        Args:
            texts: List of texts to embed. Each is redacted before it is sent.
            redactor: Identity-aware redactor, applied to every text.

        Returns:
            List of embedding vectors.

        Raises:
            EmbeddingError: If batch embedding fails.
        """
        embeddings = []
        for text in texts:
            try:
                embedding = await self.embed_text(text, redactor=redactor)
                embeddings.append(embedding)
            except EmbeddingError as e:
                logger.warning(f"Failed to embed text: {e}")
                # Continue with remaining texts
                embeddings.append([])

        if not any(embeddings):
            raise EmbeddingError("Failed to embed any texts in batch")

        return embeddings
