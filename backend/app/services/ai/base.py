"""AI provider seam (future integration point).

Gemini, LangGraph orchestration, and ChromaDB retrieval plug in behind these
interfaces. Services depend on the abstractions only, so wiring in real AI is
additive: implement a provider, register it in a factory, done.
"""

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.ai.rag import RAGService


@dataclass(frozen=True, slots=True)
class GeneratedQuestion:
    content: str
    question_type: str
    metadata: dict[str, Any] = field(default_factory=dict)


class QuestionGenerator(ABC):
    """Produces interview questions. Future impl: Gemini via LangGraph."""

    @abstractmethod
    async def initial_questions(
        self,
        *,
        target_role: str | None,
        resume_text: str | None,
        resume_id: uuid.UUID | None = None,
    ) -> list[GeneratedQuestion]: ...

    @abstractmethod
    async def follow_up(
        self, *, question: str, answer: str, resume_text: str | None
    ) -> GeneratedQuestion | None: ...


class StaticQuestionGenerator(QuestionGenerator):
    """Deterministic placeholder used until the AI pipeline lands."""

    _DEFAULTS: tuple[tuple[str, str], ...] = (
        ("Tell me about yourself and your professional background.", "behavioral"),
        ("Describe a challenging project you worked on and how you approached it.", "behavioral"),
        ("Walk me through how you would design a system relevant to your target role.", "technical"),
    )

    async def initial_questions(
        self,
        *,
        target_role: str | None,
        resume_text: str | None,
        resume_id: uuid.UUID | None = None,
    ) -> list[GeneratedQuestion]:
        return [
            GeneratedQuestion(content=c, question_type=t, metadata={"source": "static"})
            for c, t in self._DEFAULTS
        ]

    async def follow_up(
        self, *, question: str, answer: str, resume_text: str | None
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
    ) -> list[GeneratedQuestion]:
        try:
            return await self._primary.initial_questions(
                target_role=target_role, resume_text=resume_text, resume_id=resume_id
            )
        except Exception:  # noqa: BLE001 - any provider failure -> safe fallback
            return await self._fallback.initial_questions(
                target_role=target_role, resume_text=resume_text, resume_id=resume_id
            )

    async def follow_up(
        self, *, question: str, answer: str, resume_text: str | None
    ) -> GeneratedQuestion | None:
        try:
            return await self._primary.follow_up(
                question=question, answer=answer, resume_text=resume_text
            )
        except Exception:  # noqa: BLE001
            return await self._fallback.follow_up(
                question=question, answer=answer, resume_text=resume_text
            )


def get_question_generator(rag_service: "RAGService | None" = None) -> QuestionGenerator:
    """Factory.

    Returns a Gemini-backed generator (with static fallback) when
    GEMINI_API_KEY is configured; otherwise the deterministic static generator.
    
    Args:
        rag_service: Optional RAG service for retrieving relevant resume context.
    """
    from app.core.config import get_settings

    settings = get_settings()
    if settings.GEMINI_API_KEY:
        from app.services.ai.gemini import GeminiQuestionGenerator
        from app.services.ai.gemini_client import GeminiClient

        client = GeminiClient(settings.GEMINI_API_KEY, settings.GEMINI_MODEL)
        return FallbackQuestionGenerator(
            GeminiQuestionGenerator(client, rag_service=rag_service), StaticQuestionGenerator()
        )
    return StaticQuestionGenerator()
