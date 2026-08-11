"""Unit tests for question generation.

Covers GeminiQuestionGenerator parsing/validation (fake client, no network),
the StaticQuestionGenerator defaults, and the FallbackQuestionGenerator safety
net for both initial questions and follow-ups.
"""

import uuid

import pytest

from app.services.ai.base import (
    FallbackQuestionGenerator,
    GeneratedQuestion,
    InterviewSpec,
    QuestionGenerator,
    StaticQuestionGenerator,
)
from app.services.ai.gemini import GeminiQuestionGenerator
from app.services.ai.model_client import ModelError


class _FakeClient:
    def __init__(self, *, payload=None, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error
        self.calls = 0
        self.prompts: list[str] = []

    async def generate_json(self, *, system_instruction: str, prompt: str):
        self.calls += 1
        self.prompts.append(prompt)
        if self._error is not None:
            raise self._error
        return self._payload


class _FakeRag:
    """Stands in for RAGService: records queries, returns canned context."""

    def __init__(self, context: str = "", error: Exception | None = None) -> None:
        self._context = context
        self._error = error
        self.queries: list[str] = []
        self.redactors: list[object] = []
        self.purposes: list[str] = []

    async def retrieve_context(
        self, resume_id, query, top_k=5, *, redactor=None, purpose="initial_questions"
    ):
        self.queries.append(query)
        self.redactors.append(redactor)
        self.purposes.append(purpose)
        if self._error is not None:
            raise self._error
        return self._context


# -- StaticQuestionGenerator --------------------------------------------------


async def test_static_returns_default_questions():
    questions = await StaticQuestionGenerator().initial_questions(
        target_role="Backend", resume_text=None
    )
    # No spec -> the default 5-question interview.
    assert len(questions) == 5
    assert all(q.metadata["source"] == "static" for q in questions)
    # Distinct: a fallback that repeats itself reads as broken.
    assert len({q.content for q in questions}) == len(questions)


async def test_static_honours_the_requested_question_count():
    for count in (3, 5, 10):
        questions = await StaticQuestionGenerator().initial_questions(
            target_role="Backend",
            resume_text=None,
            spec=InterviewSpec(question_count=count),
        )
        assert len(questions) == count
        assert len({q.content for q in questions}) == count


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
    with pytest.raises(ModelError):
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


async def test_gemini_follow_up_includes_resume_text():
    # Regression guard: follow-ups used to be generated with no resume at all.
    client = _FakeClient(payload={"ask_follow_up": True, "content": "How so?"})
    await GeminiQuestionGenerator(client).follow_up(
        question="Q", answer="A", resume_text="Built a Redis caching layer."
    )
    assert "Built a Redis caching layer." in client.prompts[0]


async def test_gemini_follow_up_retrieves_rag_context_keyed_on_the_answer():
    client = _FakeClient(payload={"ask_follow_up": True, "content": "How so?"})
    rag = _FakeRag(context="Led the caching migration in 2024.")
    resume_id = uuid.uuid4()

    result = await GeminiQuestionGenerator(client, retriever=rag).follow_up(
        question="Tell me about performance work.",
        answer="I optimised our cache.",
        resume_text="raw resume text",
        resume_id=resume_id,
    )

    # Retrieval is keyed on the exchange, not the target role. The query is
    # rewritten before it is issued, so assert the distinctive terms survive
    # rather than the raw phrasing -- the filler ("I", "our", "tell me about")
    # is dropped on purpose.
    assert "optimised" in rag.queries[0]
    assert "cache" in rag.queries[0]
    assert "performance" in rag.queries[0]
    # Tagged so follow-up retrieval can be told from initial-question retrieval
    # in the metrics: the two run very different queries and their hit rates
    # have to be readable apart.
    assert rag.purposes[0] == "follow_up"
    # Retrieved context is preferred over the raw text.
    assert "Led the caching migration in 2024." in client.prompts[0]
    assert "raw resume text" not in client.prompts[0]
    assert result is not None
    assert result.metadata["uses_rag"] is True


async def test_gemini_falls_back_to_raw_resume_when_nothing_is_indexed():
    # Resumes uploaded before the vector index worked are in the database but
    # absent from Chroma. Retrieval returns empty -- the resume must still be
    # used, not silently dropped.
    client = _FakeClient(payload={"questions": [{"content": "Q", "question_type": "technical"}]})
    rag = _FakeRag(context="")

    questions = await GeminiQuestionGenerator(client, retriever=rag).initial_questions(
        target_role="Backend", resume_text="raw resume text", resume_id=uuid.uuid4()
    )

    assert "raw resume text" in client.prompts[0]
    assert questions[0].metadata["uses_rag"] is False


async def test_gemini_falls_back_to_raw_resume_when_retrieval_errors():
    client = _FakeClient(payload={"questions": [{"content": "Q", "question_type": "technical"}]})
    rag = _FakeRag(error=RuntimeError("chroma down"))

    questions = await GeminiQuestionGenerator(client, retriever=rag).initial_questions(
        target_role="Backend", resume_text="raw resume text", resume_id=uuid.uuid4()
    )

    assert "raw resume text" in client.prompts[0]
    assert questions[0].metadata["uses_rag"] is False


async def test_gemini_prompt_reflects_the_interview_spec():
    client = _FakeClient(payload={"questions": [{"content": "Q", "question_type": "technical"}]})
    await GeminiQuestionGenerator(client).initial_questions(
        target_role="Backend",
        resume_text=None,
        spec=InterviewSpec(
            interview_type="system_design", difficulty="senior", question_count=7
        ),
    )
    prompt = client.prompts[0]
    assert "exactly 7" in prompt
    assert "system design" in prompt.lower()
    assert "senior" in prompt.lower()


async def test_gemini_trims_overlong_question_lists_to_the_requested_count():
    # "exactly N" is a request, not a guarantee.
    client = _FakeClient(
        payload={
            "questions": [
                {"content": f"Q{i}", "question_type": "technical"} for i in range(9)
            ]
        }
    )
    questions = await GeminiQuestionGenerator(client).initial_questions(
        target_role="Backend", resume_text=None, spec=InterviewSpec(question_count=4)
    )
    assert [q.content for q in questions] == ["Q0", "Q1", "Q2", "Q3"]


async def test_gemini_keeps_a_short_question_list_rather_than_failing():
    # Fewer good questions beats discarding them and falling back to static.
    client = _FakeClient(
        payload={"questions": [{"content": "Q0", "question_type": "technical"}]}
    )
    questions = await GeminiQuestionGenerator(client).initial_questions(
        target_role="Backend", resume_text=None, spec=InterviewSpec(question_count=5)
    )
    assert len(questions) == 1


# -- FallbackQuestionGenerator ------------------------------------------------


async def test_fallback_initial_recovers_on_primary_failure():
    bad_client = _FakeClient(error=RuntimeError("boom"))
    generator = FallbackQuestionGenerator(
        GeminiQuestionGenerator(bad_client), StaticQuestionGenerator()
    )
    questions = await generator.initial_questions(target_role="Role", resume_text=None)
    # Falls back to the static defaults.
    assert len(questions) == 5
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
        async def initial_questions(self, *, target_role, resume_text, resume_id=None):
            return []

        async def follow_up(self, *, question, answer, resume_text, resume_id=None):
            raise RuntimeError("boom")

    class _FixedFallback(QuestionGenerator):
        async def initial_questions(self, *, target_role, resume_text, resume_id=None):
            return []

        async def follow_up(self, *, question, answer, resume_text, resume_id=None):
            return GeneratedQuestion(content="fb", question_type="follow_up")

    generator = FallbackQuestionGenerator(_Boom(), _FixedFallback())
    result = await generator.follow_up(question="Q", answer="A", resume_text=None)
    assert result is not None
    assert result.content == "fb"
