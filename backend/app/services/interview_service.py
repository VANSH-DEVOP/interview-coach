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
from app.repositories.report_repository import ReportRepository
from app.repositories.resume_repository import ResumeRepository
from app.schemas.interview import AnswerCreate, InterviewCreate
from app.services.ai.base import InterviewSpec, QuestionGenerator


def _utcnow() -> datetime:
    """Naive UTC timestamp matching the TIMESTAMP WITHOUT TIME ZONE columns."""
    # Compute in UTC then drop tzinfo so asyncpg accepts it for the naive
    # TIMESTAMP WITHOUT TIME ZONE columns (utcnow() is deprecated in 3.12).
    return datetime.now(timezone.utc).replace(tzinfo=None)


class InterviewService:
    def __init__(
        self,
        interviews: InterviewRepository,
        resumes: ResumeRepository,
        question_generator: QuestionGenerator,
        reports: ReportRepository,
    ) -> None:
        # No evaluator here: this service starts evaluations, it does not run
        # them. app.services.evaluation_worker owns that, on its own session.
        self.interviews = interviews
        self.resumes = resumes
        self.question_generator = question_generator
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

        await self._generate_initial_questions(session, resume_text=resume_text, start_at=1)
        return session

    async def _generate_initial_questions(
        self, session: InterviewSession, *, resume_text: str | None, start_at: int
    ) -> None:
        """Generate and persist a session's opening questions."""
        generated = await self.question_generator.initial_questions(
            target_role=session.target_role,
            resume_text=resume_text,
            resume_id=session.resume_id,
            spec=InterviewSpec(
                interview_type=session.interview_type.value,
                difficulty=session.difficulty.value,
                question_count=session.question_count,
            ),
        )
        for offset, item in enumerate(generated):
            self.interviews.session.add(
                Question(
                    session_id=session.id,
                    sequence_number=start_at + offset,
                    content=item.content,
                    question_type=QuestionType(item.question_type),
                    generation_metadata=item.metadata,
                )
            )
        await self.interviews.session.flush()

    async def _generate_follow_up(
        self,
        session: InterviewSession,
        question: Question,
        answer_content: str,
        user_id: uuid.UUID,
    ) -> None:
        """Ask for an adaptive follow-up and persist it if one comes back.

        The resume goes through so the generator can probe the answer against
        what the candidate actually claims on paper; without it the follow-up
        only ever sees the last question and answer in isolation.
        """
        follow_up = await self.question_generator.follow_up(
            question=question.content,
            answer=answer_content,
            resume_text=await self._resume_text(session, user_id),
            resume_id=session.resume_id,
        )
        if follow_up is None:
            return

        self.interviews.session.add(
            Question(
                session_id=session.id,
                parent_question_id=question.id,
                sequence_number=await self.interviews.next_sequence_number(session.id),
                content=follow_up.content,
                question_type=QuestionType.FOLLOW_UP,
                generation_metadata=follow_up.metadata,
            )
        )
        await self.interviews.session.flush()

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
        session = await self._active_session(session_id, user_id)

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
        # Answering a question the candidate had passed over reinstates it.
        question.skipped = False
        await self.interviews.session.flush()

        await self._generate_follow_up(session, question, payload.content, user_id)
        return answer

    async def _active_session(
        self, session_id: uuid.UUID, user_id: uuid.UUID, *, with_questions: bool = False
    ) -> InterviewSession:
        """An in-progress session owned by this user, or raise."""
        session = await self.interviews.get_owned(
            session_id, user_id, with_questions=with_questions
        )
        if session is None:
            raise NotFoundError("Interview session not found.")
        if session.status is not SessionStatus.IN_PROGRESS:
            raise ConflictError("This interview session is no longer in progress.")
        return session

    async def skip_question(
        self, session_id: uuid.UUID, user_id: uuid.UUID, question_id: uuid.UUID
    ) -> Question:
        """Pass over a question without answering it.

        Recorded rather than merely skipped in the UI, so an unanswered
        question the candidate chose to leave is distinguishable from one they
        never reached.
        """
        await self._active_session(session_id, user_id)

        question = await self.interviews.get_question(question_id, session_id)
        if question is None:
            raise NotFoundError("Question not found in this session.")
        if question.answer is not None:
            raise ConflictError("An answered question cannot be skipped.")

        question.skipped = True
        await self.interviews.session.flush()
        return question

    async def update_answer(
        self, session_id: uuid.UUID, user_id: uuid.UUID, payload: AnswerCreate
    ) -> Answer:
        """Replace an existing answer.

        Any follow-up generated from the previous answer is deleted and
        regenerated: it was a probe into something the candidate no longer
        said, so leaving it would leave a question referring to an answer that
        no longer exists.
        """
        session = await self._active_session(session_id, user_id)

        question = await self.interviews.get_question(payload.question_id, session_id)
        if question is None:
            raise NotFoundError("Question not found in this session.")
        if question.answer is None:
            raise ConflictError("This question has not been answered yet.")

        question.answer.content = payload.content
        question.answer.duration_seconds = payload.duration_seconds
        # Un-skip: providing an answer contradicts having passed.
        question.skipped = False

        for stale in await self.interviews.follow_ups_of(question.id):
            await self.interviews.session.delete(stale)
        await self.interviews.session.flush()

        await self._generate_follow_up(session, question, payload.content, user_id)
        return question.answer

    async def regenerate_questions(
        self, session_id: uuid.UUID, user_id: uuid.UUID
    ) -> InterviewSession:
        """Replace the question set with a freshly generated one.

        Only before anything is answered. Afterwards, discarding the questions
        would discard the answers with them -- at that point the honest action
        is a new interview, not a silent reset.
        """
        session = await self._active_session(session_id, user_id, with_questions=True)

        if any(question.answer is not None for question in session.questions):
            raise ConflictError(
                "Questions cannot be regenerated once an answer has been given."
            )

        for question in list(session.questions):
            await self.interviews.session.delete(question)
        await self.interviews.session.flush()

        await self._generate_initial_questions(
            session,
            resume_text=await self._resume_text(session, user_id),
            start_at=1,
        )

        # Expire the loaded collection before re-reading. The session object is
        # in the identity map with `questions` already populated from before the
        # delete, and SQLAlchemy will not overwrite a loaded collection on a
        # re-query -- so the response would carry the ids of questions that no
        # longer exist, which a client would render and then 404 on.
        self.interviews.session.expire(session, ["questions"])
        return await self.get_detail(session_id, user_id)

    async def abandon(self, session_id: uuid.UUID, user_id: uuid.UUID) -> InterviewSession:
        """Give up on an in-progress session without evaluating it.

        Distinct from delete: the transcript is kept, so a half-finished
        interview stays visible in history rather than vanishing. No report is
        produced, because an abandoned session has nothing worth scoring.
        """
        session = await self.interviews.get_owned(session_id, user_id)
        if session is None:
            raise NotFoundError("Interview session not found.")
        if session.status is SessionStatus.COMPLETED:
            raise ConflictError("A completed interview session cannot be abandoned.")
        if session.status is SessionStatus.ABANDONED:
            raise ConflictError("This interview session is already abandoned.")

        session.status = SessionStatus.ABANDONED
        session.completed_at = _utcnow()
        await self.interviews.session.flush()
        return session

    async def delete(self, session_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """Permanently remove a session.

        Questions, answers, and the evaluation report go with it via the
        cascade on the relationships. Allowed in any status: this is the user
        deleting their own data, not a state transition.
        """
        session = await self.interviews.get_owned(session_id, user_id)
        if session is None:
            raise NotFoundError("Interview session not found.")
        await self.interviews.delete(session)

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

        # A PENDING report, not a finished one: evaluation is a provider
        # round-trip and must not block the request that ends the interview.
        # The router hands the work off (app/services/job_queue.py) and the
        # client polls the report until it leaves PENDING/GENERATING.
        self.interviews.session.add(
            EvaluationReport(session_id=session.id, status=ReportStatus.PENDING)
        )
        # Commit, not flush. The evaluation runs on a *different* database
        # session -- another process entirely, with a queue -- and a flush is
        # invisible outside this transaction. The router hands off the moment
        # this returns, so anything less than a commit is a runner that looks up
        # the session, finds nothing, and quietly gives up with the report left
        # on PENDING forever. Committing here is deliberate: `get_session` would
        # otherwise commit only after the handoff.
        await self.interviews.session.commit()
        return session

    async def reevaluate(
        self, session_id: uuid.UUID, user_id: uuid.UUID
    ) -> EvaluationReport:
        """Queue a fresh evaluation for an already-completed session.

        Used after evaluator improvements, and to retry a report that failed.
        Resets the existing report to PENDING in place rather than creating a
        second one -- the session/report relationship is 1:1, and a new row
        would orphan whatever the user is currently looking at.

        Returns immediately with the PENDING report; the router queues the work.
        """
        session = await self.interviews.get_owned(session_id, user_id)
        if session is None:
            raise NotFoundError("Interview session not found.")
        if session.status is not SessionStatus.COMPLETED:
            raise ConflictError(
                "Only completed interview sessions can be re-evaluated."
            )

        report = await self.reports.get_owned_by_session(session_id, user_id)
        if report is None:
            report = EvaluationReport(session_id=session.id)
            self.interviews.session.add(report)

        report.status = ReportStatus.PENDING
        # The previous result is cleared deliberately: leaving stale strengths
        # and a stale score on screen beside a "generating" badge reads as if
        # the new evaluation had already produced them.
        report.overall_score = None
        report.strengths = None
        report.weaknesses = None
        report.detailed_feedback = None
        await self.interviews.session.flush()
        await self.interviews.session.refresh(report)
        # Same reasoning as complete(): the runner is on another session, so the
        # reset has to be committed before the router hands the work off.
        await self.interviews.session.commit()
        return report
