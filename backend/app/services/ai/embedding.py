"""Embedding service using Gemini API for text vectorization.

The second place text leaves for Google, and the one that is easy to forget:
indexing a resume ships the whole document a chunk at a time. Redaction happens
here for the same reason it happens in GeminiClient -- at the boundary, so no
call site can omit it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from app.services.ai.masking import Redactor, default_redactor

if TYPE_CHECKING:
    from app.services.ai.embedding_cache import EmbeddingCache

logger = logging.getLogger(__name__)

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


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

    async def _embed_uncached(self, redacted: str) -> list[float]:
        """The provider call itself. Takes already-redacted text."""
        url = f"{_BASE_URL}/{self._model}:embedContent"
        body = {
            "model": self._model,
            "content": {"parts": [{"text": redacted}]},
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                # Header, not `?key=`: httpx logs the full URL, which would put
                # the API key in cleartext in the application logs.
                response = await client.post(
                    url, headers={"x-goog-api-key": self._api_key}, json=body
                )
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"Embedding request failed: {exc}") from exc

        if response.status_code != 200:
            raise EmbeddingError(
                f"Embedding API returned HTTP {response.status_code}: {response.text[:500]}"
            )

        try:
            data = response.json()
            embedding = data["embedding"]["values"]
            if not isinstance(embedding, list):
                raise ValueError("Invalid embedding format")
            return embedding
        except (KeyError, IndexError, ValueError) as exc:
            raise EmbeddingError(f"Unexpected embedding response: {exc}") from exc

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
