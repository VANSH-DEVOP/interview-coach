"""RAG (Retrieval-Augmented Generation) service for interview questions."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.models.resume_chunk import retrieval_text as _retrieval_text
from app.services.ai import retrieval_metrics

if TYPE_CHECKING:
    from app.services.ai.masking import Redactor
    from app.services.ai.retrieval_metrics import Purpose

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Chunk:
    """One retrievable piece of a resume."""

    ordinal: int
    content: str
    section: str | None = None

    @property
    def retrieval_text(self) -> str:
        """What gets embedded and what reaches the prompt.

        Shared with `ResumeChunk` rather than reimplemented: hybrid retrieval
        fuses the dense and keyword halves by chunk text, so the string this
        produces at index time and the one the model rebuilds at query time
        have to be identical to the character.
        """
        return _retrieval_text(self.section, self.content)


# Headings a resume is likely to use. Matched case-insensitively against a
# whole line, so "Technical Skills" and "TECHNICAL SKILLS" both land.
_KNOWN_SECTIONS = frozenset(
    {
        "about",
        "achievements",
        "additional information",
        "awards",
        "certifications",
        "certificates",
        "contact",
        "education",
        "employment",
        "employment history",
        "experience",
        "interests",
        "languages",
        "objective",
        "profile",
        "projects",
        "publications",
        "references",
        "skills",
        "summary",
        "technical skills",
        "volunteer",
        "volunteering",
        "work experience",
        "work history",
    }
)

# A heading is short. Anything longer is a sentence that happens to be
# capitalised, and treating it as a heading would swallow the text under it.
_MAX_HEADING_CHARS = 48

# Target size for a chunk. Sections shorter than this stay whole; longer ones
# are split at line boundaries. Bigger than the 500 it replaces, because a
# chunk that keeps a whole job or a whole skills list together retrieves as one
# idea, and the section heading now travels with every piece.
_MAX_CHUNK_CHARS = 800


def _is_heading(line: str) -> bool:
    """Whether a line looks like a resume section heading.

    Two signals, deliberately conservative -- a false positive splits a section
    in half, and a false negative merely leaves a chunk unlabelled:

    - it is one of the headings resumes actually use, or
    - it is short, has letters, and is entirely upper case.
    """
    stripped = line.strip().rstrip(":").strip()
    if not stripped or len(stripped) > _MAX_HEADING_CHARS:
        return False
    if stripped.lower() in _KNOWN_SECTIONS:
        return True
    letters = [character for character in stripped if character.isalpha()]
    return bool(letters) and stripped == stripped.upper()


class ResumeChunker:
    """Split a resume into chunks that follow its own structure.

    The chunker this replaces packed paragraphs to a character budget and then
    faked overlap by prepending the previous chunk's last 100 characters behind
    a "\\n...\\n" marker, which retrieval stripped back out. So the same
    sentences were embedded twice, could be retrieved twice, and cost prompt
    budget as a copy of themselves.

    Sections are the unit instead. A resume already declares its own structure
    in headings, and those headings are the strongest retrieval signal in the
    document: "what did they study" wants the block under EDUCATION, and no
    amount of character counting will find it.
    """

    def __init__(self, max_chunk_chars: int = _MAX_CHUNK_CHARS) -> None:
        self._max_chunk_chars = max_chunk_chars

    def chunk(self, text: str) -> list[Chunk]:
        if not text or not text.strip():
            return []

        chunks: list[Chunk] = []
        for section, body in self._sections(text):
            for piece in self._split(body):
                chunks.append(Chunk(ordinal=len(chunks), content=piece, section=section))
        return chunks

    def _sections(self, text: str) -> list[tuple[str | None, str]]:
        """Split into (heading, body) pairs, in document order.

        Text before the first heading -- the name and contact block, usually --
        keeps a heading of None rather than being dropped or attached to
        whatever section happens to come first.
        """
        sections: list[tuple[str | None, str]] = []
        heading: str | None = None
        body: list[str] = []

        for line in text.replace("\r\n", "\n").split("\n"):
            if _is_heading(line):
                if any(entry.strip() for entry in body):
                    sections.append((heading, "\n".join(body).strip()))
                heading = line.strip().rstrip(":").strip()
                body = []
            else:
                body.append(line.rstrip())

        if any(entry.strip() for entry in body):
            sections.append((heading, "\n".join(body).strip()))
        return sections

    def _split(self, body: str) -> list[str]:
        """Break one section's body into pieces no larger than the budget.

        Paragraphs are the unit, not lines. Resume text arrives from a PDF
        hard-wrapped at whatever width the document used, so a physical line is
        a typographic accident: splitting on one cut "Introduced gRPC between
        the routing and dispatch services, cutting p99 latency" from "from
        340ms to 45ms", and a query about latency then matched neither half
        well. Blank lines are the real boundaries -- one job, one bullet group,
        one paragraph.

        A single paragraph over the budget is left whole. It is rare in a
        resume, and half a job entry retrieves as neither of the two things it
        was.
        """
        if len(body) <= self._max_chunk_chars:
            return [body] if body.strip() else []

        pieces: list[str] = []
        current: list[str] = []
        size = 0
        for paragraph in body.split("\n\n"):
            if not paragraph.strip():
                continue
            addition = len(paragraph) + 2
            if current and size + addition > self._max_chunk_chars:
                pieces.append("\n\n".join(current).strip())
                current, size = [], 0
            current.append(paragraph.strip())
            size += addition

        if current:
            pieces.append("\n\n".join(current).strip())
        return [piece for piece in pieces if piece]


class RAGService:
    """Retrieval-Augmented Generation service for contextual interview questions."""

    def __init__(self, embedding_service: Any, vector_store: Any) -> None:
        self._embedding_service = embedding_service
        self._vector_store = vector_store

    async def index_chunks(
        self,
        resume_id: uuid.UUID,
        user_id: uuid.UUID,
        chunks: list[Chunk],
        *,
        redactor: "Redactor | None" = None,
    ) -> list[int]:
        """Embed chunks and put them in the vector store.

        Takes chunks rather than raw text: chunking is a pure function and the
        rows belong to the caller's transaction, so `ResumeService` splits the
        text and saves it, and this stays free of a database session. It is
        cached process-wide (`get_rag_service`) and could not hold one anyway.

        Args:
            resume_id: Resume ID.
            user_id: User ID.
            chunks: Chunks to index, in document order.
            redactor: Applied to each chunk before it is sent for embedding.

        Returns:
            The ordinals that reached the vector store. Anything missing from
            this list is text the retriever cannot see, and the caller records
            that on the row.

        Raises:
            RuntimeError: If indexing fails.
        """
        if not chunks:
            logger.warning(f"No chunks to index for resume {resume_id}")
            return []

        embedded: list[int] = []
        try:
            with retrieval_metrics.timed() as elapsed:
                embeddings = await self._embedding_service.embed_batch(
                    [chunk.retrieval_text for chunk in chunks], redactor=redactor
                )

                # An embedding call can fail per chunk without failing the
                # batch, and the caller needs to know which ones.
                usable = [
                    (chunk, embedding)
                    for chunk, embedding in zip(chunks, embeddings)
                    if embedding
                ]
                if not usable:
                    raise RuntimeError(f"Failed to embed any chunks for resume {resume_id}")

                await self._vector_store.add_resume(
                    resume_id,
                    user_id,
                    [chunk.retrieval_text for chunk, _ in usable],
                    [embedding for _, embedding in usable],
                )
                embedded = [chunk.ordinal for chunk, _ in usable]
                logger.info(
                    f"Successfully indexed {len(embedded)} chunks for resume {resume_id}"
                )
            return embedded
        except Exception as e:
            logger.error(f"Failed to index resume {resume_id}: {e}")
            raise RuntimeError(f"Resume indexing failed: {e}") from e
        finally:
            # Recorded even when indexing raised: a resume that produced 30
            # chunks and embedded 4 is the case worth seeing, and it reaches
            # here by both paths.
            retrieval_metrics.record_indexing(
                chunks_produced=len(chunks),
                chunks_embedded=len(embedded),
                duration_ms=elapsed[0],
            )

    async def retrieve_ranked(
        self,
        resume_id: uuid.UUID,
        query: str,
        top_k: int = 5,
        *,
        redactor: "Redactor | None" = None,
        max_distance: float | None = None,
        purpose: "Purpose" = "initial_questions",
    ) -> list[str]:
        """Dense retrieval, best first, with the junk cut off.

        `max_distance` drops chunks the embedding model does not think are
        related at all. Without it `top_k` always returns k chunks: a query
        about underwater basket weaving fills the prompt with the k least-bad
        paragraphs of a payments resume, and the caller has no scores with
        which to notice.

        The cutoff is deliberately coarse and configurable, because the right
        value depends on the embedding model -- distances from one model's
        "unrelated" band overlap another's "related" one. It is a guard against
        obvious junk, not a relevance judgement.
        """
        results = await self._retrieve(
            resume_id, query, top_k=top_k, redactor=redactor, purpose=purpose
        )
        if not results.documents:
            return []

        distances = results.distances or [0.0] * len(results.documents)
        # Strictly less than, not at most: cosine distance 1.0 is exactly
        # orthogonal -- no relation at all -- so a cutoff of 1.0 meaning "keep
        # things no better than unrelated" would keep precisely the chunks it
        # exists to drop. A query sharing nothing with a chunk lands on 1.0 on
        # the nose, which is how this surfaced.
        kept = [
            document
            for document, distance in zip(results.documents, distances)
            if max_distance is None or distance < max_distance
        ]
        if len(kept) < len(results.documents):
            logger.debug(
                "Dropped %d chunk(s) beyond the distance cutoff for resume %s.",
                len(results.documents) - len(kept),
                resume_id,
            )
        return [document.replace("\n...\n", "\n") for document in kept]

    async def retrieve_context(
        self,
        resume_id: uuid.UUID,
        query: str,
        top_k: int = 5,
        *,
        redactor: "Redactor | None" = None,
        purpose: "Purpose" = "initial_questions",
    ) -> str:
        """Dense-only retrieval, as one string.

        Kept for callers with no database session to run keyword search from.
        `HybridRetriever` is the path a request takes.
        """
        documents = await self.retrieve_ranked(
            resume_id, query, top_k=top_k, redactor=redactor, purpose=purpose
        )
        return "\n\n".join(documents)

    async def _retrieve(
        self,
        resume_id: uuid.UUID,
        query: str,
        *,
        top_k: int,
        redactor: "Redactor | None",
        purpose: "Purpose",
    ) -> Any:
        """Embed the query, ask the vector store, and record what happened.

        Raises:
            RuntimeError: If retrieval fails.
        """
        try:
            with retrieval_metrics.timed() as elapsed:
                query_embedding = await self._embedding_service.embed_text(
                    query, redactor=redactor
                )
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
            return results

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
        logger.debug(
            f"Retrieved {len(results.documents)} chunks for query in resume {resume_id}"
        )
        return results

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
