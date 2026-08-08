"""Interview session business logic.

Question generation is delegated to the QuestionGenerator seam: the static
placeholder today, Gemini + LangGraph later, with no changes to this service's
public surface.
"""

import uuid
from datetime import datetime, timezone


def _utcnow() -> datetime:
    """Naive UTC timestamp matching the TIMESTAMP WITHOUT TIME ZONE columns."""
    # Compute in UTC then drop tzinfo so asyncpg accepts it for the naive
    # TIMESTAMP WITHOUT TIME ZONE columns (utcnow() is deprecated in 3.12).
    return datetime.now(timezone.utc).replace(tzinfo=None)

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.models.answer import Answer
from app.models.evaluation_report import EvaluationReport, ReportStatus
from app.models.interview_session import InterviewSession, SessionStatus
from app.models.question import Question, QuestionType
from app.repositories.interview_repository import InterviewRepository
from app.repositories.report_repository import ReportRepository
from app.repositories.resume_repository import ResumeRepository
from app.schemas.interview import AnswerCreate, InterviewCreate
from app.services.ai.base import InterviewSpec, QuestionGenerator
from app.services.ai.evaluator import EvaluationResult, Evaluator, QAPair


class InterviewService:
    def __init__(
        self,
        interviews: InterviewRepository,
        resumes: ResumeRepository,
        question_generator: QuestionGenerator,
        evaluator: Evaluator,
        reports: ReportRepository,
    ) -> None:
        self.interviews = interviews
        self.resumes = resumes
        self.question_generator = question_generator
        self.evaluator = evaluator
        self.reports = reports

    async def _resume_text(
        self, session: InterviewSession, user_id: uuid.UUID
    ) -> str | None:
        """Parsed text of the resume attached to a session, if any.

        Ownership is re-checked rather than trusted: the resume is fetched
        through get_owned like every other user-scoped read.
        """
        if session.resume_id is None:
            return None
        resume = await self.resumes.get_owned(session.resume_id, user_id)
        return resume.parsed_text if resume is not None else None

    async def _evaluate_session(self, session: InterviewSession) -> EvaluationResult:
        """Run the evaluator over a session's transcript."""
        transcript = [
            QAPair(
                question=q.content,
                answer=q.answer.content if q.answer is not None else None,
            )
            for q in session.questions
        ]
        return await self.evaluator.evaluate(
            target_role=session.target_role, transcript=transcript
        )

    async def create(self, user_id: uuid.UUID, payload: InterviewCreate) -> InterviewSession:
        resume_text: str | None = None
        if payload.resume_id is not None:
            resume = await self.resumes.get_owned(payload.resume_id, user_id)
            if resume is None:
                raise NotFoundError("Resume not found.")
            # parsed_text is populated by the future resume-parsing pipeline; when
            # present it personalizes question generation.
            resume_text = resume.parsed_text

        session = InterviewSession(
            user_id=user_id,
            resume_id=payload.resume_id,
            title=payload.title,
            target_role=payload.target_role,
            status=SessionStatus.IN_PROGRESS,
            interview_type=payload.interview_type,
            difficulty=payload.difficulty,
            question_count=payload.question_count,
            started_at=_utcnow(),
        )
        session = await self.interviews.add(session)

        generated = await self.question_generator.initial_questions(
            target_role=payload.target_role,
            resume_text=resume_text,
            resume_id=payload.resume_id,
            spec=InterviewSpec(
                interview_type=payload.interview_type.value,
                difficulty=payload.difficulty.value,
                question_count=payload.question_count,
            ),
        )
        for index, item in enumerate(generated, start=1):
            self.interviews.session.add(
                Question(
                    session_id=session.id,
                    sequence_number=index,
                    content=item.content,
                    question_type=QuestionType(item.question_type),
                    generation_metadata=item.metadata,
                )
            )
        await self.interviews.session.flush()
        return session

    async def list(
        self, user_id: uuid.UUID, *, status: SessionStatus | None, offset: int, limit: int
    ):
        return await self.interviews.list_for_user(
            user_id, status=status, offset=offset, limit=limit
        )

    async def get_detail(self, session_id: uuid.UUID, user_id: uuid.UUID) -> InterviewSession:
        session = await self.interviews.get_owned(session_id, user_id, with_questions=True)
        if session is None:
            raise NotFoundError("Interview session not found.")
        return session

    async def submit_answer(
        self, session_id: uuid.UUID, user_id: uuid.UUID, payload: AnswerCreate
    ) -> Answer:
        session = await self.interviews.get_owned(session_id, user_id)
        if session is None:
            raise NotFoundError("Interview session not found.")
        if session.status is not SessionStatus.IN_PROGRESS:
            raise ConflictError("This interview session is not accepting answers.")

        question = await self.interviews.get_question(payload.question_id, session_id)
        if question is None:
            raise NotFoundError("Question not found in this session.")
        if question.answer is not None:
            raise ConflictError("This question has already been answered.")

        answer = Answer(
            question_id=question.id,
            content=payload.content,
            duration_seconds=payload.duration_seconds,
        )
        self.interviews.session.add(answer)
        await self.interviews.session.flush()

        # AI seam: adaptive follow-up. The static generator returns None.
        # The resume goes through so the generator can probe the answer against
        # what the candidate actually claims on paper; without it the follow-up
        # only ever sees the last question and answer in isolation.
        follow_up = await self.question_generator.follow_up(
            question=question.content,
            answer=payload.content,
            resume_text=await self._resume_text(session, user_id),
            resume_id=session.resume_id,
        )
        if follow_up is not None:
            next_seq = await self.interviews.next_sequence_number(session_id)
            self.interviews.session.add(
                Question(
                    session_id=session.id,
                    parent_question_id=question.id,
                    sequence_number=next_seq,
                    content=follow_up.content,
                    question_type=QuestionType.FOLLOW_UP,
                    generation_metadata=follow_up.metadata,
                )
            )
            await self.interviews.session.flush()

        return answer

    async def complete(self, session_id: uuid.UUID, user_id: uuid.UUID) -> InterviewSession:
        session = await self.interviews.get_owned(session_id, user_id, with_questions=True)
        if session is None:
            raise NotFoundError("Interview session not found.")
        if session.status is SessionStatus.COMPLETED:
            raise ConflictError("This interview session is already completed.")
        if session.status is SessionStatus.ABANDONED:
            raise ValidationError("An abandoned session cannot be completed.")

        session.status = SessionStatus.COMPLETED
        session.completed_at = _utcnow()

        # Evaluate the transcript and persist a completed report. The evaluator
        # uses Gemini when configured and a deterministic heuristic otherwise,
        # so a populated report is always produced.
        result = await self._evaluate_session(session)
        self.interviews.session.add(
            EvaluationReport(
                session_id=session.id,
                status=ReportStatus.COMPLETED,
                overall_score=result.overall_score,
                strengths=result.strengths,
                weaknesses=result.weaknesses,
                detailed_feedback=result.detailed_feedback,
            )
        )
        await self.interviews.session.flush()
        return session

    async def reevaluate(
        self, session_id: uuid.UUID, user_id: uuid.UUID
    ) -> EvaluationReport:
        """Regenerate the evaluation report for an already-completed session.

        Useful after evaluator improvements so users can refresh an existing
        report without re-running the interview. Updates the report in place if
        one exists, otherwise creates it.
        """
        session = await self.interviews.get_owned(
            session_id, user_id, with_questions=True
        )
        if session is None:
            raise NotFoundError("Interview session not found.")
        if session.status is not SessionStatus.COMPLETED:
            raise ConflictError(
                "Only completed interview sessions can be re-evaluated."
            )

        result = await self._evaluate_session(session)
        report = await self.reports.get_owned_by_session(session_id, user_id)
        if report is None:
            report = EvaluationReport(session_id=session.id)
            self.interviews.session.add(report)

        report.status = ReportStatus.COMPLETED
        report.overall_score = result.overall_score
        report.strengths = result.strengths
        report.weaknesses = result.weaknesses
        report.detailed_feedback = result.detailed_feedback
        await self.interviews.session.flush()
        await self.interviews.session.refresh(report)
        return report
