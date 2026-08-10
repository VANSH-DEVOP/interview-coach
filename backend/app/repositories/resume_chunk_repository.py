"""Queries over the retrievable pieces of a resume."""

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Executable, delete, select, update
from sqlalchemy.engine import CursorResult

from app.core.time import utcnow
from app.models.resume_chunk import ResumeChunk
from app.repositories.base import BaseRepository


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
