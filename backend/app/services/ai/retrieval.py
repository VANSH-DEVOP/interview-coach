"""Hybrid retrieval: dense vectors and Postgres full-text, fused.

Dense retrieval generalises. It can match "distributed streaming systems" to a
paragraph that never says either word, which is the entire reason for embedding
a resume in the first place. What it is bad at is exact tokens: an embedding is
a lossy summary, so a rare, decisive term -- `gRPC`, `Kubernetes`, a version
number, a library nobody else lists -- can be averaged away inside a long
chunk, and a query naming it lands somewhere plausible instead.

Keyword search is the mirror image: exact where dense is fuzzy, useless the
moment the query paraphrases. Resumes are keyword-dense documents and
interviewers ask paraphrased questions, so the pipeline needs both.

They are fused with Reciprocal Rank Fusion rather than by mixing scores.
Cosine distances and `ts_rank` values are not comparable -- different ranges,
different distributions, neither calibrated -- so any weighted sum of them
encodes an arbitrary exchange rate. RRF throws the magnitudes away and keeps
only the ordering each retriever produced, which is the part each is actually
reliable about.

The other thing this adds is the ability to return *nothing*. `top_k` on its
own always yields k chunks, however irrelevant, and the caller got a string
with no scores in it and no way to tell. A query about nothing now retrieves
nothing, and question generation falls back to the resume text -- visibly, via
`retrieval_metrics`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from app.core.config import get_settings
from app.services.ai import retrieval_metrics

if TYPE_CHECKING:
    from app.models.resume_chunk import ResumeChunk
    from app.services.ai.masking import Redactor
    from app.services.ai.rag import RAGService
    from app.services.ai.retrieval_metrics import Purpose

logger = logging.getLogger(__name__)

# The constant in Reciprocal Rank Fusion, from the paper that introduced it
# (Cormack et al., 2009). It damps the difference between the top ranks so one
# retriever's confident first place cannot bury a chunk both retrievers liked;
# 60 is the value the literature settled on and there is no local reason to
# differ.
_RRF_K = 60


class ChunkSearcher(Protocol):
    """The sparse half. Satisfied by ResumeChunkRepository."""

    async def search(
        self, resume_id: uuid.UUID, query: str, *, limit: int = 5
    ) -> list[tuple["ResumeChunk", float]]: ...


class Retriever(Protocol):
    """What question generation needs: text for a query, or nothing.

    Satisfied by `HybridRetriever` and, dense-only, by `RAGService` -- which is
    what lets the evaluation worker and any other sessionless caller retrieve
    without a keyword index.
    """

    async def retrieve_context(
        self,
        resume_id: uuid.UUID,
        query: str,
        top_k: int = 5,
        *,
        redactor: "Redactor | None" = None,
        purpose: "Purpose" = "initial_questions",
    ) -> str: ...


@dataclass(frozen=True)
class Scored:
    """One candidate chunk and where each retriever placed it."""

    text: str
    score: float
    dense_rank: int | None = None
    sparse_rank: int | None = None

    @property
    def sources(self) -> str:
        if self.dense_rank is not None and self.sparse_rank is not None:
            return "both"
        return "dense" if self.dense_rank is not None else "sparse"


def fuse(
    dense: list[str], sparse: list[str], *, limit: int, k: int = _RRF_K
) -> list[Scored]:
    """Reciprocal Rank Fusion over two ranked lists of chunk text.

    A chunk found by both retrievers scores the sum of its two contributions,
    so agreement outranks either retriever's private favourite -- which is the
    whole point of running two.
    """
    ranks: dict[str, dict[str, int]] = {}
    for source, ranking in (("dense", dense), ("sparse", sparse)):
        for position, text in enumerate(ranking, start=1):
            ranks.setdefault(text, {})[source] = position

    scored = [
        Scored(
            text=text,
            score=sum(1.0 / (k + position) for position in positions.values()),
            dense_rank=positions.get("dense"),
            sparse_rank=positions.get("sparse"),
        )
        for text, positions in ranks.items()
    ]
    # Ties broken by the dense rank, then the sparse one, so the order is
    # deterministic rather than dependent on dict iteration.
    scored.sort(
        key=lambda candidate: (
            -candidate.score,
            candidate.dense_rank or 10**6,
            candidate.sparse_rank or 10**6,
        )
    )
    return scored[:limit]


class HybridRetriever:
    """Dense + keyword retrieval for one request.

    Per-request, unlike `RAGService`: the keyword half is a repository bound to
    the request's session, while the dense half is a process-wide client. This
    composes the two so question generation sees one `retrieve_context`.

    Degrades to whichever half is available. A broken vector store still leaves
    keyword search, a resume indexed before chunks were rows still has vectors,
    and each is better than the truncated-resume fallback.
    """

    def __init__(self, rag_service: "RAGService | None", chunks: ChunkSearcher | None) -> None:
        self._rag = rag_service
        self._chunks = chunks

    async def retrieve_context(
        self,
        resume_id: uuid.UUID,
        query: str,
        top_k: int = 5,
        *,
        redactor: "Redactor | None" = None,
        purpose: "Purpose" = "initial_questions",
    ) -> str:
        """Retrieve resume context for a query. Returns "" when nothing fits."""
        fused = await self.retrieve_scored(
            resume_id, query, top_k=top_k, redactor=redactor, purpose=purpose
        )
        return "\n\n".join(candidate.text for candidate in fused)

    async def retrieve_scored(
        self,
        resume_id: uuid.UUID,
        query: str,
        top_k: int = 5,
        *,
        redactor: "Redactor | None" = None,
        purpose: "Purpose" = "initial_questions",
    ) -> list[Scored]:
        """The ranked candidates, with which retriever found each.

        The scored form exists because the string one cannot be judged: a
        caller handed "" and a caller handed five weak paragraphs had no way to
        tell the difference, and neither did the benchmark.
        """
        settings = get_settings()
        # Each half is asked for more than the final count, so fusion has
        # something to disagree about; k results in means k results out and RRF
        # would only reorder them.
        candidates = top_k * 2

        dense: list[str] = []
        dense_failure: Exception | None = None
        if self._rag is not None:
            try:
                dense = await self._rag.retrieve_ranked(
                    resume_id,
                    query,
                    top_k=candidates,
                    redactor=redactor,
                    max_distance=settings.RAG_MAX_DISTANCE,
                )
            except Exception as exc:  # noqa: BLE001 - keyword search may still work
                dense_failure = exc
                logger.warning(
                    "Dense retrieval failed for resume %s; continuing with keyword "
                    "search only: %s",
                    resume_id,
                    exc,
                )

        sparse: list[str] = []
        if self._chunks is not None:
            try:
                matches = await self._chunks.search(resume_id, query, limit=candidates)
                sparse = [chunk.retrieval_text for chunk, _ in matches]
            except Exception as exc:  # noqa: BLE001 - dense results may still work
                logger.warning(
                    "Keyword retrieval failed for resume %s: %s", resume_id, exc
                )

        if not dense and not sparse:
            if dense_failure is not None and self._chunks is None:
                # Nothing worked and nothing else could have. The caller's
                # fallback to raw resume text is the right outcome, but this is
                # a failure rather than an empty index and is recorded as one
                # by RAGService already.
                raise RuntimeError("Retrieval failed") from dense_failure
            return []

        fused = fuse(dense, sparse, limit=top_k)
        retrieval_metrics.record_fusion(
            purpose=purpose,
            dense=len(dense),
            sparse=len(sparse),
            fused=len(fused),
            agreed=sum(1 for candidate in fused if candidate.sources == "both"),
        )

        return fused
