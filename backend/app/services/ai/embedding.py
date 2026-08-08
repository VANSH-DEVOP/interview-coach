"""Embedding service using Gemini API for text vectorization."""

from __future__ import annotations

import logging
from typing import Any

import httpx

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
    ) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._model = model

    async def embed_text(self, text: str) -> list[float]:
        """Generate embedding for a single text.

        Args:
            text: Text to embed.

        Returns:
            Embedding vector as list[float].

        Raises:
            EmbeddingError: If embedding generation fails.
        """
        if not text or not text.strip():
            raise EmbeddingError("Cannot embed empty text.")

        url = f"{_BASE_URL}/{self._model}:embedContent"
        body = {
            "model": self._model,
            "content": {"parts": [{"text": text}]},
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

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts.

        Args:
            texts: List of texts to embed.

        Returns:
            List of embedding vectors.

        Raises:
            EmbeddingError: If batch embedding fails.
        """
        embeddings = []
        for text in texts:
            try:
                embedding = await self.embed_text(text)
                embeddings.append(embedding)
            except EmbeddingError as e:
                logger.warning(f"Failed to embed text: {e}")
                # Continue with remaining texts
                embeddings.append([])

        if not any(embeddings):
            raise EmbeddingError("Failed to embed any texts in batch")

        return embeddings
