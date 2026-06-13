"""Service-layer integration test for the create -> answer -> complete flow.

Exercises InterviewService end-to-end with in-memory fake repositories (no DB,
dialect-independent). Verifies that:
  * initial questions are generated and persisted,
  * answers attach to questions,
  * completing the session produces a POPULATED, COMPLETED report,
  * timestamps are naive UTC (regression guard for the asyncpg
    "offset-naive vs offset-aware" fix).
"""

import uuid
from datetime import datetime, timezone

import pytest

from app.models.answer import Answer
from app.models.evaluation_report import EvaluationReport, ReportStatus
from app.models.interview_session import InterviewSession, SessionStatus
from app.models.question import Question
from app.schemas.interview import AnswerCreate, InterviewCreate
from app.services.ai.base import StaticQuestionGenerator
from app.services.ai.evaluator import HeuristicEvaluator
from app.services.interview_service import InterviewService


class _FakeSession:
    """Minimal stand-in for AsyncSession used by the service.

    Tracks added entities and assigns ids / wires relationships so subsequent
    repository reads behave like a real unit of work.
    """

    def __init__(self) -> None:
        self.questions: dict[uuid.UUID, Question] = {}
        self.answers: list[Answer] = []
        self.reports: list[EvaluationReport] = []

    def add(self, entity) -> None:
        if isinstance(entity, Question):
            if entity.id is None:
                entity.id = uuid.uuid4()
            entity.answer = None
            self.questions[entity.id] = entity
        elif isinstance(entity, Answer):
            if entity.id is None:
                entity.id = uuid.uuid4()
            if entity.created_at is None:
                entity.created_at = datetime.now(timezone.utc).replace(tzinfo=None)
            self.answers.append(entity)
            question = self.questions.get(entity.question_id)
            if question is not None:
                question.answer = entity
        elif isinstance(entity, EvaluationReport):
            if entity.id is None:
                entity.id = uuid.uuid4()
            self.reports.append(entity)

    async def flush(self) -> None:
        return None

    async def refresh(self, entity) -> None:
        return None


class _FakeInterviewRepository:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self._sessions: dict[uuid.UUID, InterviewSession] = {}

    async def add(self, entity: InterviewSession) -> InterviewSession:
        if entity.id is None:
            entity.id = uuid.uuid4()
        entity.questions = []
        self._sessions[entity.id] = entity
        return entity

    async def get_owned(self, session_id, user_id, *, with_questions=False):
        session = self._sessions.get(session_id)
        if session is None or session.user_id != user_id:
            return None
        # Reflect any questions added to the unit of work.
        session.questions = sorted(
            (q for q in self.session.questions.values() if q.session_id == session_id),
            key=lambda q: q.sequence_number,
        )
        return session

    async def get_question(self, question_id, session_id):
        question = self.session.questions.get(question_id)
        if question is None or question.session_id != session_id:
            return None
        return question


class _FakeResumeRepository:
    async def get_owned(self, resume_id, user_id):  # pragma: no cover - unused path
        return None


class _FakeReportRepository:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    async def get_owned_by_session(self, session_id, user_id):
        return next(
            (r for r in self.session.reports if r.session_id == session_id), None
        )


@pytest.fixture
def service() -> InterviewService:
    fake_session = _FakeSession()
    interviews = _FakeInterviewRepository(fake_session)
    resumes = _FakeResumeRepository()
    reports = _FakeReportRepository(fake_session)
    return InterviewService(
        interviews,
        resumes,
        StaticQuestionGenerator(),
        HeuristicEvaluator(),
        reports,
    )


async def test_full_interview_flow_produces_completed_report(service):
    user_id = uuid.uuid4()

    # 1. Create -> session is in progress with generated questions.
    session = await service.create(
        user_id,
        InterviewCreate(title="Backend practice", target_role="Backend Engineer"),
    )
    assert session.status is SessionStatus.IN_PROGRESS
    detail = await service.get_detail(session.id, user_id)
    assert len(detail.questions) == 3

    # started_at must be a naive UTC datetime (regression guard).
    assert session.started_at is not None
    assert session.started_at.tzinfo is None

    # 2. Answer every question.
    for question in detail.questions:
        answer = await service.submit_answer(
            session.id,
            user_id,
            AnswerCreate(
                question_id=question.id,
                content="A thorough, detailed answer with concrete examples.",
            ),
        )
        assert isinstance(answer, Answer)

    # 3. Complete -> a populated, COMPLETED report is created.
    completed = await service.complete(session.id, user_id)
    assert completed.status is SessionStatus.COMPLETED
    assert completed.completed_at is not None
    assert completed.completed_at.tzinfo is None  # naive UTC

    reports = service.interviews.session.reports
    assert len(reports) == 1
    report = reports[0]
    assert report.status is ReportStatus.COMPLETED
    assert report.overall_score is not None
    assert report.overall_score > 0
    assert report.strengths  # non-empty
    assert "per_question" in report.detailed_feedback
    assert len(report.detailed_feedback["per_question"]) == 3


async def test_complete_twice_raises_conflict(service):
    from app.core.exceptions import ConflictError

    user_id = uuid.uuid4()
    session = await service.create(
        user_id, InterviewCreate(title="One-shot", target_role=None)
    )
    await service.complete(session.id, user_id)
    with pytest.raises(ConflictError):
        await service.complete(session.id, user_id)


async def test_complete_unknown_session_raises_not_found(service):
    from app.core.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        await service.complete(uuid.uuid4(), uuid.uuid4())


async def test_reevaluate_updates_existing_report_in_place(service):
    user_id = uuid.uuid4()
    session = await service.create(
        user_id, InterviewCreate(title="Re-eval", target_role="Backend")
    )
    detail = await service.get_detail(session.id, user_id)
    for question in detail.questions:
        await service.submit_answer(
            session.id,
            user_id,
            AnswerCreate(question_id=question.id, content="A detailed answer."),
        )
    await service.complete(session.id, user_id)

    report = await service.reevaluate(session.id, user_id)
    assert report.status is ReportStatus.COMPLETED
    assert report.overall_score is not None
    # No duplicate report was created; the existing one was updated in place.
    assert len(service.interviews.session.reports) == 1


async def test_reevaluate_requires_completed_session(service):
    from app.core.exceptions import ConflictError

    user_id = uuid.uuid4()
    session = await service.create(
        user_id, InterviewCreate(title="In progress", target_role=None)
    )
    with pytest.raises(ConflictError):
        await service.reevaluate(session.id, user_id)


async def test_reevaluate_unknown_session_raises_not_found(service):
    from app.core.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        await service.reevaluate(uuid.uuid4(), uuid.uuid4())
