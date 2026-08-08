"""Tests for AI degradation reporting.

The fallback wrappers deliberately swallow every provider exception so an
interview is always completable. The cost is that a dead model looks exactly
like a healthy one -- which is how a retired embedding model went unnoticed.
These tests pin the observability that makes a degradation visible.
"""

import pytest

from app.services.ai import degradation
from app.services.ai.base import (
    FallbackQuestionGenerator,
    GeneratedQuestion,
    QuestionGenerator,
    StaticQuestionGenerator,
)
from app.services.ai.evaluator import (
    Evaluator,
    EvaluationResult,
    FallbackEvaluator,
    HeuristicEvaluator,
    QAPair,
)


@pytest.fixture(autouse=True)
def _clean_state():
    degradation.reset()
    yield
    degradation.reset()


class _BrokenGenerator(QuestionGenerator):
    async def initial_questions(self, *, target_role, resume_text, resume_id=None):
        raise RuntimeError("HTTP 404: model retired")

    async def follow_up(self, *, question, answer, resume_text):
        raise RuntimeError("HTTP 429: quota exceeded")


class _BrokenEvaluator(Evaluator):
    async def evaluate(self, *, target_role, transcript) -> EvaluationResult:
        raise RuntimeError("HTTP 503: provider unavailable")


async def test_snapshot_starts_clean():
    assert degradation.snapshot() == {
        "fallbacks": 0,
        "last_operation": None,
        "last_error": None,
        "last_at": None,
    }


async def test_initial_questions_fallback_is_recorded():
    generator = FallbackQuestionGenerator(_BrokenGenerator(), StaticQuestionGenerator())

    questions = await generator.initial_questions(target_role="Backend", resume_text=None)

    # The safety net still works: the caller gets usable questions.
    assert len(questions) == 3
    assert all(isinstance(q, GeneratedQuestion) for q in questions)

    # ...but the degradation is no longer invisible.
    snap = degradation.snapshot()
    assert snap["fallbacks"] == 1
    assert snap["last_operation"] == "initial_questions"
    assert "model retired" in str(snap["last_error"])
    assert snap["last_at"] is not None


async def test_follow_up_fallback_is_recorded():
    generator = FallbackQuestionGenerator(_BrokenGenerator(), StaticQuestionGenerator())

    assert await generator.follow_up(question="Q", answer="A", resume_text=None) is None

    snap = degradation.snapshot()
    assert snap["fallbacks"] == 1
    assert snap["last_operation"] == "follow_up"
    assert "quota exceeded" in str(snap["last_error"])


async def test_evaluator_fallback_is_recorded():
    evaluator = FallbackEvaluator(_BrokenEvaluator(), HeuristicEvaluator())

    result = await evaluator.evaluate(
        target_role="Backend",
        transcript=[QAPair(question="Tell me about yourself.", answer="I build APIs.")],
    )

    assert result.overall_score is not None
    snap = degradation.snapshot()
    assert snap["fallbacks"] == 1
    assert snap["last_operation"] == "evaluate"


async def test_fallbacks_accumulate():
    generator = FallbackQuestionGenerator(_BrokenGenerator(), StaticQuestionGenerator())

    await generator.initial_questions(target_role=None, resume_text=None)
    await generator.follow_up(question="Q", answer="A", resume_text=None)

    assert degradation.snapshot()["fallbacks"] == 2


async def test_healthy_provider_records_nothing():
    generator = FallbackQuestionGenerator(StaticQuestionGenerator(), StaticQuestionGenerator())

    await generator.initial_questions(target_role=None, resume_text=None)

    assert degradation.snapshot()["fallbacks"] == 0


async def test_health_endpoint_reports_ai_state(client):
    before = await client.get("/api/v1/health")
    assert before.status_code == 200
    assert before.json()["ai"]["fallbacks"] == 0

    generator = FallbackQuestionGenerator(_BrokenGenerator(), StaticQuestionGenerator())
    await generator.initial_questions(target_role=None, resume_text=None)

    after = await client.get("/api/v1/health")
    ai = after.json()["ai"]
    assert ai["fallbacks"] == 1
    assert ai["last_operation"] == "initial_questions"
    # A degraded AI provider must not fail the liveness probe.
    assert after.json()["status"] == before.json()["status"]
