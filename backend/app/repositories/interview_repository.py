import uuid

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.interview_session import InterviewSession, SessionStatus
from app.models.question import Question
from app.repositories.base import BaseRepository


class InterviewRepository(BaseRepository[InterviewSession]):
    model = InterviewSession

    async def get_owned(
        self, session_id: uuid.UUID, user_id: uuid.UUID, *, with_questions: bool = False
    ) -> InterviewSession | None:
        stmt = select(InterviewSession).where(
            InterviewSession.id == session_id, InterviewSession.user_id == user_id
        )
        if with_questions:
            stmt = stmt.options(selectinload(InterviewSession.questions).selectinload(Question.answer))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_user(
        self,
        user_id: uuid.UUID,
        *,
        status: SessionStatus | None,
        offset: int,
        limit: int,
    ) -> tuple[list[InterviewSession], int]:
        where = [InterviewSession.user_id == user_id]
        if status is not None:
            where.append(InterviewSession.status == status)
        stmt = (
            select(InterviewSession)
            .where(*where)
            .order_by(InterviewSession.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        items = list((await self.session.execute(stmt)).scalars().all())
        total = await self.count(*where)
        return items, total

    async def get_question(
        self, question_id: uuid.UUID, session_id: uuid.UUID
    ) -> Question | None:
        stmt = (
            select(Question)
            .where(Question.id == question_id, Question.session_id == session_id)
            .options(selectinload(Question.answer))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()
