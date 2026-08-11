import uuid

from sqlalchemy import select

from app.models.resume import Resume
from app.repositories.base import BaseRepository


class ResumeRepository(BaseRepository[Resume]):
    model = Resume

    async def get_owned(self, resume_id: uuid.UUID, user_id: uuid.UUID) -> Resume | None:
        stmt = select(Resume).where(Resume.id == resume_id, Resume.user_id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def count_for_user(self, user_id: uuid.UUID) -> int:
        """How many resumes this account holds. The occupancy quota reads it.

        A live count rather than a stored tally: deleting a resume has to free
        the quota, and a tally is a second copy of a fact that already exists.
        """
        return await self.count(Resume.user_id == user_id)

    async def all_for_user(self, user_id: uuid.UUID) -> list[Resume]:
        """Every resume, unpaginated. For account deletion.

        Deliberately not the paginated method: deleting a page at a time would
        leave blobs and vector-store entries behind for anyone with more
        resumes than the page size.
        """
        stmt = select(Resume).where(Resume.user_id == user_id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_user(
        self, user_id: uuid.UUID, *, offset: int, limit: int
    ) -> tuple[list[Resume], int]:
        stmt = (
            select(Resume)
            .where(Resume.user_id == user_id)
            .order_by(Resume.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        items = list((await self.session.execute(stmt)).scalars().all())
        total = await self.count(Resume.user_id == user_id)
        return items, total
