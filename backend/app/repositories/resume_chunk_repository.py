"""Queries over the retrievable pieces of a resume."""

import re
import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Executable, delete, func, select, update
from sqlalchemy.engine import CursorResult

from app.core.time import utcnow
from app.models.resume_chunk import ResumeChunk
from app.repositories.base import BaseRepository

# Query text is turned into lexemes here rather than handed to Postgres whole.
# `plainto_tsquery` ANDs every term, so "skills and experience relevant to
# Senior Backend Engineer" would match only a chunk containing all of them --
# which is no chunk. Retrieval wants OR with ranking by how much matched, the
# way BM25 behaves, so the terms are joined with `|`.
_WORD = re.compile(r"[A-Za-z0-9+#.]+")


def _to_tsquery(query: str) -> str | None:
    """Build an OR-of-terms tsquery from free text, or None if there is none.

    Tokenising in Python keeps this safe: `to_tsquery` has a syntax and would
    raise on a stray `&` or an unbalanced bracket in a candidate's answer,
    which is exactly the sort of input that reaches follow-up retrieval.
    Only word characters survive, and the result is bound as a parameter, so
    nothing here is interpolated into SQL.
    """
    terms = [term for term in _WORD.findall(query) if len(term) > 1]
    return " | ".join(terms) if terms else None


class ResumeChunkRepository(BaseRepository[ResumeChunk]):
    model = ResumeChunk

    async def replace_for_resume(
        self, resume_id: uuid.UUID, user_id: uuid.UUID, chunks: list[ResumeChunk]
    ) -> list[ResumeChunk]:
        """Swap a resume's chunks for a new set.

        Delete-then-insert rather than upsert-by-ordinal: re-chunking the same
        resume can produce *fewer* pieces than last time, and updating in place
        would leave the tail of the previous run behind as rows that no longer
        correspond to any part of the document.
        """
        await self.delete_for_resume(resume_id)
        for chunk in chunks:
            chunk.resume_id = resume_id
            chunk.user_id = user_id
            self.session.add(chunk)
        await self.session.flush()
        return chunks

    async def delete_for_resume(self, resume_id: uuid.UUID) -> int:
        return await self._execute_rowcount(
            delete(ResumeChunk).where(ResumeChunk.resume_id == resume_id)
        )

    async def list_for_resume(self, resume_id: uuid.UUID) -> list[ResumeChunk]:
        result = await self.session.execute(
            select(ResumeChunk)
            .where(ResumeChunk.resume_id == resume_id)
            .order_by(ResumeChunk.ordinal)
        )
        return list(result.scalars().all())

    async def mark_embedded(
        self, resume_id: uuid.UUID, ordinals: list[int], *, at: datetime | None = None
    ) -> int:
        """Record which chunks reached the vector store.

        The complement is the point: a row with content and no `embedded_at` is
        a piece of the resume retrieval cannot see, and before this table that
        state was a counter that died with the process.
        """
        if not ordinals:
            return 0
        return await self._execute_rowcount(
            update(ResumeChunk)
            .where(
                ResumeChunk.resume_id == resume_id,
                ResumeChunk.ordinal.in_(ordinals),
            )
            .values(embedded_at=at or utcnow())
        )

    async def search(
        self, resume_id: uuid.UUID, query: str, *, limit: int = 5
    ) -> list[tuple[ResumeChunk, float]]:
        """Keyword search within one resume, best first.

        The sparse half of hybrid retrieval. Dense search generalises -- it can
        match "distributed streaming" to a paragraph about Kafka -- and pays
        for it by being approximate about exact tokens, so a rare term like
        "gRPC" or a version number can be diluted away in a long chunk. This
        half does the opposite, and the two are fused rather than chosen
        between.

        Returns (chunk, ts_rank) pairs. Chunks matching no term are absent
        rather than returned with a low score, which is what lets a query about
        nothing retrieve nothing.
        """
        tsquery = _to_tsquery(query)
        if tsquery is None:
            return []

        query_expression = func.to_tsquery("english", tsquery)
        rank = func.ts_rank(ResumeChunk.search_vector, query_expression)
        result = await self.session.execute(
            select(ResumeChunk, rank.label("rank"))
            .where(
                ResumeChunk.resume_id == resume_id,
                ResumeChunk.search_vector.op("@@")(query_expression),
            )
            .order_by(rank.desc(), ResumeChunk.ordinal)
            .limit(limit)
        )
        return [(chunk, float(score)) for chunk, score in result.all()]

    async def count_unembedded(self, resume_id: uuid.UUID) -> int:
        return await self.count(
            ResumeChunk.resume_id == resume_id,
            ResumeChunk.embedded_at.is_(None),
        )

    async def _execute_rowcount(self, statement: Executable) -> int:
        """Run a bulk UPDATE/DELETE and report how many rows it touched."""
        result = cast(CursorResult[Any], await self.session.execute(statement))
        await self.session.flush()
        return result.rowcount or 0
