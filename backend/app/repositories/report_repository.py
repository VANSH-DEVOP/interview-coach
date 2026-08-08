import uuid
from datetime import datetime
from typing import NamedTuple

from sqlalchemy import Row, select
from sqlalchemy.orm import selectinload

from app.models.evaluation_report import EvaluationReport
from app.models.interview_session import InterviewSession
from app.repositories.base import BaseRepository


class ScoreHistoryRow(NamedTuple):
    """One scored session, for progress tracking."""

    session_id: uuid.UUID
    title: str
    target_role: str | None
    interview_type: str
    difficulty: str
    score: float
    scored_at: datetime


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

    async def get_owned_with_session(
        self, report_id: uuid.UUID, user_id: uuid.UUID
    ) -> EvaluationReport | None:
        """A report with its session eagerly loaded.

        Export needs the session's title and role. Reaching through
        report.session without this raises MissingGreenlet under asyncio.
        """
        stmt = (
            select(EvaluationReport)
            .join(InterviewSession, EvaluationReport.session_id == InterviewSession.id)
            .where(EvaluationReport.id == report_id, InterviewSession.user_id == user_id)
            .options(selectinload(EvaluationReport.session))
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

    async def score_history(
        self, user_id: uuid.UUID, *, limit: int = 50
    ) -> list[ScoreHistoryRow]:
        """The user's scored sessions, oldest first.

        Only rows with a score are returned: a pending or failed report is not
        a data point, and plotting it as zero would invent a dip that never
        happened.

        The `limit` is applied to the *most recent* rows and the result is then
        reversed, so a long history yields the latest N in chronological order
        rather than the first N.
        """
        stmt = (
            select(
                InterviewSession.id,
                InterviewSession.title,
                InterviewSession.target_role,
                InterviewSession.interview_type,
                InterviewSession.difficulty,
                EvaluationReport.overall_score,
                EvaluationReport.created_at,
            )
            .join(InterviewSession, EvaluationReport.session_id == InterviewSession.id)
            .where(
                InterviewSession.user_id == user_id,
                EvaluationReport.overall_score.is_not(None),
            )
            .order_by(EvaluationReport.created_at.desc())
            .limit(limit)
        )
        rows: list[Row] = list((await self.session.execute(stmt)).all())
        return [
            ScoreHistoryRow(
                session_id=row[0],
                title=row[1],
                target_role=row[2],
                interview_type=row[3].value if hasattr(row[3], "value") else str(row[3]),
                difficulty=row[4].value if hasattr(row[4], "value") else str(row[4]),
                score=float(row[5]),
                scored_at=row[6],
            )
            for row in reversed(rows)
        ]

    async def feedback_history(
        self, user_id: uuid.UUID, *, limit: int = 50
    ) -> tuple[list, list]:
        """Every strength and weakness the user has been given, flattened.

        Returns (strengths, weaknesses) as raw JSONB contents; callers are
        responsible for coercing them, since the column holds whatever the
        model produced.
        """
        stmt = (
            select(EvaluationReport.strengths, EvaluationReport.weaknesses)
            .join(InterviewSession, EvaluationReport.session_id == InterviewSession.id)
            .where(InterviewSession.user_id == user_id)
            .order_by(EvaluationReport.created_at.desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).all()

        strengths: list = []
        weaknesses: list = []
        for row in rows:
            strengths.extend(row[0] or [])
            weaknesses.extend(row[1] or [])
        return strengths, weaknesses

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

        from sqlalchemy import func
        from sqlalchemy import select as sa_select

        count_stmt = (
            sa_select(func.count())
            .select_from(EvaluationReport)
            .join(InterviewSession, EvaluationReport.session_id == InterviewSession.id)
            .where(InterviewSession.user_id == user_id)
        )
        total = (await self.session.execute(count_stmt)).scalar_one()
        return items, total
