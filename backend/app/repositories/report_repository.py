import uuid

from sqlalchemy import select

from app.models.evaluation_report import EvaluationReport
from app.models.interview_session import InterviewSession
from app.repositories.base import BaseRepository


class ReportRepository(BaseRepository[EvaluationReport]):
    model = EvaluationReport

    async def get_owned(
        self, report_id: uuid.UUID, user_id: uuid.UUID
    ) -> EvaluationReport | None:
        stmt = (
            select(EvaluationReport)
            .join(InterviewSession, EvaluationReport.session_id == InterviewSession.id)
            .where(EvaluationReport.id == report_id, InterviewSession.user_id == user_id)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_owned_by_session(
        self, session_id: uuid.UUID, user_id: uuid.UUID
    ) -> EvaluationReport | None:
        stmt = (
            select(EvaluationReport)
            .join(InterviewSession, EvaluationReport.session_id == InterviewSession.id)
            .where(
                EvaluationReport.session_id == session_id,
                InterviewSession.user_id == user_id,
            )
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_user(
        self, user_id: uuid.UUID, *, offset: int, limit: int
    ) -> tuple[list[EvaluationReport], int]:
        base = (
            select(EvaluationReport)
            .join(InterviewSession, EvaluationReport.session_id == InterviewSession.id)
            .where(InterviewSession.user_id == user_id)
        )
        stmt = base.order_by(EvaluationReport.created_at.desc()).offset(offset).limit(limit)
        items = list((await self.session.execute(stmt)).scalars().all())

        from sqlalchemy import func, select as sa_select

        count_stmt = (
            sa_select(func.count())
            .select_from(EvaluationReport)
            .join(InterviewSession, EvaluationReport.session_id == InterviewSession.id)
            .where(InterviewSession.user_id == user_id)
        )
        total = (await self.session.execute(count_stmt)).scalar_one()
        return items, total
