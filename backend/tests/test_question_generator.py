"""Unit tests for question generation.

Covers GeminiQuestionGenerator parsing/validation (fake client, no network),
the StaticQuestionGenerator defaults, and the FallbackQuestionGenerator safety
net for both initial questions and follow-ups.
"""

import pytest

from app.services.ai.base import (
    FallbackQuestionGenerator,
    GeneratedQuestion,
    QuestionGenerator,
    StaticQuestionGenerator,
)
from app.services.ai.gemini import GeminiQuestionGenerator
from app.services.ai.gemini_client import GeminiError


class _FakeClient:
    def __init__(self, *, payload=None, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error
        self.calls = 0

    async def generate_json(self, *, system_instruction: str, prompt: str):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._payload


# -- StaticQuestionGenerator --------------------------------------------------


async def test_static_returns_default_questions():
    questions = await StaticQuestionGenerator().initial_questions(
        target_role="Backend", resume_text=None
    )
    assert len(questions) == 3
    assert all(q.metadata["source"] == "static" for q in questions)


async def test_static_follow_up_is_none():
    result = await StaticQuestionGenerator().follow_up(
        question="Q", answer="A", resume_text=None
    )
    assert result is None


# -- GeminiQuestionGenerator --------------------------------------------------


async def test_gemini_parses_and_tags_questions():
    client = _FakeClient(
        payload={
            "questions": [
                {"content": "Tell me about X", "question_type": "behavioral"},
                {"content": "Design Y", "question_type": "technical"},
            ]
        }
    )
    questions = await GeminiQuestionGenerator(client).initial_questions(
        target_role="Backend", resume_text="resume body"
    )
    assert [q.content for q in questions] == ["Tell me about X", "Design Y"]
    assert {q.question_type for q in questions} == {"behavioral", "technical"}
    assert all(q.metadata["source"] == "gemini" for q in questions)


async def test_gemini_normalizes_invalid_type_to_behavioral():
    client = _FakeClient(
        payload={"questions": [{"content": "Q", "question_type": "nonsense"}]}
    )
    questions = await GeminiQuestionGenerator(client).initial_questions(
        target_role=None, resume_text=None
    )
    assert questions[0].question_type == "behavioral"


async def test_gemini_skips_empty_content_and_raises_when_all_empty():
    client = _FakeClient(payload={"questions": [{"content": "  ", "question_type": "technical"}]})
    with pytest.raises(GeminiError):
        await GeminiQuestionGenerator(client).initial_questions(
            target_role=None, resume_text=None
        )


async def test_gemini_follow_up_returns_question_when_requested():
    client = _FakeClient(payload={"ask_follow_up": True, "content": "Why that approach?"})
    result = await GeminiQuestionGenerator(client).follow_up(
        question="Q", answer="A", resume_text=None
    )
    assert result is not None
    assert result.question_type == "follow_up"
    assert result.content == "Why that approach?"


async def test_gemini_follow_up_returns_none_when_not_requested():
    client = _FakeClient(payload={"ask_follow_up": False, "content": "ignored"})
    result = await GeminiQuestionGenerator(client).follow_up(
        question="Q", answer="A", resume_text=None
    )
    assert result is None


# -- FallbackQuestionGenerator ------------------------------------------------


async def test_fallback_initial_recovers_on_primary_failure():
    bad_client = _FakeClient(error=RuntimeError("boom"))
    generator = FallbackQuestionGenerator(
        GeminiQuestionGenerator(bad_client), StaticQuestionGenerator()
    )
    questions = await generator.initial_questions(target_role="Role", resume_text=None)
    # Falls back to the 3 static defaults.
    assert len(questions) == 3
    assert all(q.metadata["source"] == "static" for q in questions)


async def test_fallback_initial_uses_primary_when_successful():
    client = _FakeClient(
        payload={"questions": [{"content": "Primary Q", "question_type": "technical"}]}
    )
    generator = FallbackQuestionGenerator(
        GeminiQuestionGenerator(client), StaticQuestionGenerator()
    )
    questions = await generator.initial_questions(target_role="Role", resume_text=None)
    assert len(questions) == 1
    assert questions[0].content == "Primary Q"


async def test_fallback_follow_up_recovers_on_primary_failure():
    class _Boom(QuestionGenerator):
        async def initial_questions(self, *, target_role, resume_text):
            return []

        async def follow_up(self, *, question, answer, resume_text):
            raise RuntimeError("boom")

    class _FixedFallback(QuestionGenerator):
        async def initial_questions(self, *, target_role, resume_text):
            return []

        async def follow_up(self, *, question, answer, resume_text):
            return GeneratedQuestion(content="fb", question_type="follow_up")

    generator = FallbackQuestionGenerator(_Boom(), _FixedFallback())
    result = await generator.follow_up(question="Q", answer="A", resume_text=None)
    assert result is not None
    assert result.content == "fb"
