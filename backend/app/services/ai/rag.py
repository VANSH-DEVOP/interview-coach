"""RAG (Retrieval-Augmented Generation) service for interview questions."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from app.services.ai import retrieval_metrics

if TYPE_CHECKING:
    from app.services.ai.masking import Redactor
    from app.services.ai.retrieval_metrics import Purpose

logger = logging.getLogger(__name__)


class TextChunker:
    """Split resume text into semantic chunks."""

    @staticmethod
    def chunk_text(
        text: str,
        chunk_size: int = 500,
        overlap: int = 100,
    ) -> list[str]:
        """Split text into chunks with overlap.

        Args:
            text: Text to chunk.
            chunk_size: Target size of each chunk in characters.
            overlap: Overlap between chunks.

        Returns:
            List of text chunks.
        """
        if not text or not text.strip():
            return []

        # Split by paragraphs first for semantic integrity
        paragraphs = text.split("\n\n")
        chunks = []
        current_chunk = ""

        for para in paragraphs:
            if len(current_chunk) + len(para) + 2 < chunk_size:
                # Add to current chunk if it doesn't exceed size
                current_chunk += ("\n\n" if current_chunk else "") + para
            else:
                # Start new chunk if adding this para would exceed size
                if current_chunk:
                    chunks.append(current_chunk)
                current_chunk = para

        # Add remaining chunk
        if current_chunk:
            chunks.append(current_chunk)

        # Add overlap between chunks for context continuity
        if len(chunks) > 1 and overlap > 0:
            overlapped_chunks = []
            for i, chunk in enumerate(chunks):
                if i == 0:
                    overlapped_chunks.append(chunk)
                else:
                    # Add end of previous chunk as prefix for context
                    prev_end = chunks[i - 1][-overlap:] if len(chunks[i - 1]) > overlap else chunks[i - 1]
                    combined = prev_end + "\n...\n" + chunk
                    overlapped_chunks.append(combined)
            chunks = overlapped_chunks

        return [c.strip() for c in chunks if c.strip()]


class RAGService:
    """Retrieval-Augmented Generation service for contextual interview questions."""

    def __init__(self, embedding_service: Any, vector_store: Any) -> None:
        self._embedding_service = embedding_service
        self._vector_store = vector_store

    async def index_resume(
        self,
        resume_id: uuid.UUID,
        user_id: uuid.UUID,
        resume_text: str,
        *,
        redactor: "Redactor | None" = None,
    ) -> int:
        """Index a resume for RAG retrieval.

        Args:
            resume_id: Resume ID.
            user_id: User ID.
            resume_text: Full parsed resume text.
            redactor: Applied to each chunk before it is sent for embedding.

        Returns:
            Number of chunks stored.

        Raises:
            RuntimeError: If indexing fails.
        """
        produced = 0
        embedded = 0
        try:
            with retrieval_metrics.timed() as elapsed:
                # Chunk the resume
                chunks = TextChunker.chunk_text(resume_text)
                produced = len(chunks)
                if not chunks:
                    logger.warning(f"No chunks generated for resume {resume_id}")
                    return 0

                logger.info(f"Chunked resume {resume_id} into {len(chunks)} pieces")

                # Generate embeddings for chunks
                embeddings = await self._embedding_service.embed_batch(
                    chunks, redactor=redactor
                )

                # Filter out failed embeddings
                valid_chunks = [
                    (chunk, embedding)
                    for chunk, embedding in zip(chunks, embeddings)
                    if embedding
                ]

                if not valid_chunks:
                    raise RuntimeError(f"Failed to embed any chunks for resume {resume_id}")

                chunks_only = [chunk for chunk, _ in valid_chunks]
                embeddings_only = [embedding for _, embedding in valid_chunks]
                embedded = len(chunks_only)

                # Store in vector store
                await self._vector_store.add_resume(
                    resume_id, user_id, chunks_only, embeddings_only
                )

                logger.info(
                    f"Successfully indexed {len(chunks_only)} chunks for resume {resume_id}"
                )
            return embedded
        except Exception as e:
            logger.error(f"Failed to index resume {resume_id}: {e}")
            raise RuntimeError(f"Resume indexing failed: {e}") from e
        finally:
            # Recorded even when indexing raised: a resume that produced 30
            # chunks and embedded 4 is the case worth seeing, and it reaches
            # here by both paths.
            if produced:
                retrieval_metrics.record_indexing(
                    chunks_produced=produced,
                    chunks_embedded=embedded,
                    duration_ms=elapsed[0],
                )

    async def retrieve_context(
        self,
        resume_id: uuid.UUID,
        query: str,
        top_k: int = 5,
        *,
        redactor: "Redactor | None" = None,
        purpose: "Purpose" = "initial_questions",
    ) -> str:
        """Retrieve relevant resume context for a query.

        Args:
            resume_id: Resume ID.
            query: Query string (e.g., interview question).
            top_k: Number of top results to retrieve.
            redactor: Applied to the query before it is sent for embedding.
                A follow-up query is the candidate's own answer text, so it is
                no less sensitive than the resume it is matched against.

        Returns:
            Concatenated context from top-k relevant chunks.

        Raises:
            RuntimeError: If retrieval fails.
        """
        try:
            with retrieval_metrics.timed() as elapsed:
                # Embed the query
                query_embedding = await self._embedding_service.embed_text(
                    query, redactor=redactor
                )

                # Retrieve similar chunks
                results = await self._vector_store.retrieve_relevant(
                    query_embedding, resume_id, top_k=top_k
                )
        except Exception as e:
            retrieval_metrics.record_retrieval(
                purpose=purpose,
                outcome="failed",
                duration_ms=elapsed[0],
                error=f"{type(e).__name__}: {e}",
            )
            logger.error(f"Failed to retrieve context for resume {resume_id}: {e}")
            raise RuntimeError(f"Context retrieval failed: {e}") from e

        if not results.documents:
            retrieval_metrics.record_retrieval(
                purpose=purpose, outcome="empty", duration_ms=elapsed[0]
            )
            logger.warning(f"No relevant chunks found for query in resume {resume_id}")
            return ""

        # The best distance is recorded, not just the count: five chunks at
        # cosine distance 1.9 are five irrelevant chunks, and a "hit" that is
        # only ever counted looks exactly like a good one.
        retrieval_metrics.record_retrieval(
            purpose=purpose,
            outcome="hit",
            duration_ms=elapsed[0],
            chunks=len(results.documents),
            best_distance=min(results.distances) if results.distances else None,
        )

        # Combine chunks into context, removing overlap markers
        context_parts = []
        for doc in results.documents:
            cleaned = doc.replace("\n...\n", "\n")
            context_parts.append(cleaned)

        context = "\n\n".join(context_parts)
        logger.debug(
            f"Retrieved {len(results.documents)} chunks for query in resume {resume_id}"
        )
        return context

    async def delete_index(self, resume_id: uuid.UUID) -> None:
        """Delete indexed resume from vector store.

        Args:
            resume_id: Resume ID.
        """
        try:
            await self._vector_store.delete_resume(resume_id)
            logger.info(f"Deleted index for resume {resume_id}")
        except Exception as e:
            logger.error(f"Failed to delete index for resume {resume_id}: {e}")
            # Don't raise; deletion failure shouldn't block the flow
