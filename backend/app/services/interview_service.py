"""Interview session business logic.

Question generation is delegated to the QuestionGenerator seam: the static
placeholder today, Gemini + LangGraph later, with no changes to this service's
public surface.
"""

import uuid
from datetime import datetime, timezone

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.models.answer import Answer
from app.models.evaluation_report import EvaluationReport, ReportStatus
from app.models.interview_session import InterviewSession, SessionStatus
from app.models.question import Question, QuestionType
from app.repositories.interview_repository import InterviewRepository
from app.repositories.resume_repository import ResumeRepository
from app.schemas.interview import AnswerCreate, InterviewCreate
from app.services.ai.base import QuestionGenerator


class InterviewService:
    def __init__(
        self,
        interviews: InterviewRepository,
        resumes: ResumeRepository,
        question_generator: QuestionGenerator,
    ) -> None:
        self.interviews = interviews
        self.resumes = resumes
        self.question_generator = question_generator

    async def create(self, user_id: uuid.UUID, payload: InterviewCreate) -> InterviewSession:
        if payload.resume_id is not None:
            resume = await self.resumes.get_owned(payload.resume_id, user_id)
            if resume is None:
                raise NotFoundError("Resume not found.")

        session = InterviewSession(
            user_id=user_id,
            resume_id=payload.resume_id,
            title=payload.title,
            target_role=payload.target_role,
            status=SessionStatus.IN_PROGRESS,
            started_at=datetime.now(timezone.utc),
        )
        session = await self.interviews.add(session)

        generated = await self.question_generator.initial_questions(
            target_role=payload.target_role, resume_text=None
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

        # AI seam: adaptive follow-up. The static generator returns None;
        # the LangGraph pipeline will return real follow-up questions here.
        follow_up = await self.question_generator.follow_up(
            question=question.content, answer=payload.content, resume_text=None
        )
        if follow_up is not None:
            next_seq = len(session.questions) + 1 if session.questions else 1
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
        session = await self.interviews.get_owned(session_id, user_id)
        if session is None:
            raise NotFoundError("Interview session not found.")
        if session.status is SessionStatus.COMPLETED:
            raise ConflictError("This interview session is already completed.")
        if session.status is SessionStatus.ABANDONED:
            raise ValidationError("An abandoned session cannot be completed.")

        session.status = SessionStatus.COMPLETED
        session.completed_at = datetime.now(timezone.utc)

        # Evaluation report stub: created as PENDING; the future AI pipeline
        # picks pending reports up and fills in scores and feedback.
        self.interviews.session.add(
            EvaluationReport(session_id=session.id, status=ReportStatus.PENDING)
        )
        await self.interviews.session.flush()
        return session
