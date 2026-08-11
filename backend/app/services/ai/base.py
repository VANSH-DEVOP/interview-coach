"""AI provider seam (future integration point).

Gemini, LangGraph orchestration, and ChromaDB retrieval plug in behind these
interfaces. Services depend on the abstractions only, so wiring in real AI is
additive: implement a provider, register it in a factory, done.
"""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.services.ai.degradation import record_attempt, record_fallback

if TYPE_CHECKING:
    from app.services.ai.masking import Redactor
    from app.services.ai.retrieval import Retriever


@dataclass(frozen=True, slots=True)
class GeneratedQuestion:
    content: str
    question_type: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InterviewSpec:
    """What the user asked for: shape, seniority, and length.

    Grouped into one object rather than three more keyword arguments so the
    generator seam stays readable as configuration grows. Plain strings, not
    the model enums, to keep app.services.ai free of ORM imports.
    """

    interview_type: str = "mixed"
    difficulty: str = "mid"
    question_count: int = 5


class QuestionGenerator(ABC):
    """Produces interview questions. Future impl: Gemini via LangGraph."""

    @abstractmethod
    async def initial_questions(
        self,
        *,
        target_role: str | None,
        resume_text: str | None,
        resume_id: uuid.UUID | None = None,
        spec: "InterviewSpec | None" = None,
    ) -> list[GeneratedQuestion]: ...

    @abstractmethod
    async def follow_up(
        self,
        *,
        question: str,
        answer: str,
        resume_text: str | None,
        resume_id: uuid.UUID | None = None,
    ) -> GeneratedQuestion | None: ...


class StaticQuestionGenerator(QuestionGenerator):
    """Deterministic placeholder used until the AI pipeline lands."""

    # Ten distinct questions, enough to satisfy the largest allowed
    # question_count without repeating. Asking the same question twice reads as
    # broken; returning fewer than requested only reads as brief.
    _DEFAULTS: tuple[tuple[str, str], ...] = (
        ("Tell me about yourself and your professional background.", "behavioral"),
        ("Describe a challenging project you worked on and how you approached it.", "behavioral"),
        ("Walk me through how you would design a system relevant to your target role.", "technical"),
        ("Tell me about a time you disagreed with a teammate. How did it resolve?", "behavioral"),
        ("Describe a bug that took you a long time to find. How did you track it down?", "technical"),
        ("How do you decide what to prioritise when everything seems urgent?", "behavioral"),
        ("Walk me through how you would debug a service that is slow under load.", "technical"),
        ("Tell me about something you built that you are proud of, and why.", "behavioral"),
        ("How do you approach testing work you are not confident about?", "technical"),
        ("Describe a time you received difficult feedback. What did you do with it?", "behavioral"),
    )

    async def initial_questions(
        self,
        *,
        target_role: str | None,
        resume_text: str | None,
        resume_id: uuid.UUID | None = None,
        spec: "InterviewSpec | None" = None,
    ) -> list[GeneratedQuestion]:
        # Honour the requested count as far as the fixed pool allows. The pool
        # covers the full allowed range, so this only ever truncates.
        spec = spec or InterviewSpec()
        return [
            GeneratedQuestion(content=c, question_type=t, metadata={"source": "static"})
            for c, t in self._DEFAULTS[: spec.question_count]
        ]

    async def follow_up(
        self,
        *,
        question: str,
        answer: str,
        resume_text: str | None,
        resume_id: uuid.UUID | None = None,
    ) -> GeneratedQuestion | None:
        # Adaptive follow-ups arrive with the LangGraph integration.
        return None


class FallbackQuestionGenerator(QuestionGenerator):
    """Tries a primary generator (e.g. Gemini); falls back on any failure.

    Guarantees the interview flow keeps working even when the AI provider is
    unconfigured, rate-limited, or returns malformed data.
    """

    def __init__(self, primary: QuestionGenerator, fallback: QuestionGenerator) -> None:
        self._primary = primary
        self._fallback = fallback

    async def initial_questions(
        self,
        *,
        target_role: str | None,
        resume_text: str | None,
        resume_id: uuid.UUID | None = None,
        spec: "InterviewSpec | None" = None,
    ) -> list[GeneratedQuestion]:
        record_attempt("initial_questions")
        try:
            return await self._primary.initial_questions(
                target_role=target_role,
                resume_text=resume_text,
                resume_id=resume_id,
                spec=spec,
            )
        except Exception as exc:  # noqa: BLE001 - any provider failure -> safe fallback
            record_fallback("initial_questions", exc)
            return await self._fallback.initial_questions(
                target_role=target_role,
                resume_text=resume_text,
                resume_id=resume_id,
                spec=spec,
            )

    async def follow_up(
        self,
        *,
        question: str,
        answer: str,
        resume_text: str | None,
        resume_id: uuid.UUID | None = None,
    ) -> GeneratedQuestion | None:
        record_attempt("follow_up")
        try:
            return await self._primary.follow_up(
                question=question,
                answer=answer,
                resume_text=resume_text,
                resume_id=resume_id,
            )
        except Exception as exc:  # noqa: BLE001
            record_fallback("follow_up", exc)
            return await self._fallback.follow_up(
                question=question,
                answer=answer,
                resume_text=resume_text,
                resume_id=resume_id,
            )


def get_question_generator(
    retriever: "Retriever | None" = None,
    redactor: "Redactor | None" = None,
) -> QuestionGenerator:
    """Factory.

    Returns a Gemini-backed generator (with static fallback) when
    GEMINI_API_KEY is configured; otherwise the deterministic static generator.

    Args:
        retriever: Optional retriever for relevant resume context. A
            `HybridRetriever` in a request, which needs a session for its
            keyword half; `RAGService` alone is dense-only and works anywhere.
        redactor: Identity-aware redactor for the account holder whose resume
            this will read. Omitting it does not disable redaction -- the
            boundaries fall back to patterns alone -- it only means the
            candidate's own name is not recognised as theirs.
    """
    from app.core.config import get_settings

    settings = get_settings()
    if settings.GEMINI_API_KEY:
        from app.services.ai.gemini import GeminiQuestionGenerator
        from app.services.ai.model_client import ModelClient

        client = ModelClient(
            settings.GEMINI_API_KEY, settings.GEMINI_MODEL, redactor=redactor
        )
        return FallbackQuestionGenerator(
            GeminiQuestionGenerator(client, retriever=retriever, redactor=redactor),
            StaticQuestionGenerator(),
        )
    return StaticQuestionGenerator()
