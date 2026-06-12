"""AI provider seam (future integration point).

Gemini, LangGraph orchestration, and ChromaDB retrieval plug in behind these
interfaces. Services depend on the abstractions only, so wiring in real AI is
additive: implement a provider, register it in a factory, done.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class GeneratedQuestion:
    content: str
    question_type: str
    metadata: dict[str, Any] = field(default_factory=dict)


class QuestionGenerator(ABC):
    """Produces interview questions. Future impl: Gemini via LangGraph."""

    @abstractmethod
    async def initial_questions(
        self, *, target_role: str | None, resume_text: str | None
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
        self, *, target_role: str | None, resume_text: str | None
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


def get_question_generator() -> QuestionGenerator:
    """Factory; swaps to GeminiQuestionGenerator when AI integration lands."""
    return StaticQuestionGenerator()
