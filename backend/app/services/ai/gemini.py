"""Gemini-backed question generation.

Activated only when GEMINI_API_KEY is configured. On any API failure it raises
GeminiError; the factory wraps this generator so the static generator can take
over, guaranteeing the interview flow always works.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from app.services.ai.base import GeneratedQuestion, QuestionGenerator
from app.services.ai.gemini_client import GeminiClient

if TYPE_CHECKING:
    from app.services.ai.rag import RAGService

logger = logging.getLogger(__name__)

_VALID_TYPES = {"behavioral", "technical", "follow_up"}

_SYSTEM = (
    "You are an expert technical interviewer. You generate concise, high-signal "
    "interview questions and always respond with valid JSON only."
)


class GeminiQuestionGenerator(QuestionGenerator):
    def __init__(self, client: GeminiClient, rag_service: "RAGService | None" = None) -> None:
        self._client = client
        self._rag_service = rag_service

    async def _resume_context(
        self,
        *,
        resume_text: str | None,
        resume_id: uuid.UUID | None,
        query: str,
    ) -> tuple[str, bool]:
        """Build the resume section of a prompt, preferring RAG over truncation.

        Returns (prompt_fragment, used_rag). Falls back to truncated raw text
        whenever retrieval is unavailable, fails, *or returns nothing* -- the
        last case matters for resumes uploaded before the vector index worked,
        which are in the database but absent from Chroma. Dropping the resume
        entirely there would silently de-personalise the interview.
        """
        if self._rag_service and resume_id and resume_text:
            try:
                context = await self._rag_service.retrieve_context(
                    resume_id, query, top_k=5
                )
            except Exception as e:
                logger.warning(
                    "RAG retrieval failed for resume %s; using truncated resume text: %s",
                    resume_id,
                    e,
                )
            else:
                if context:
                    logger.info(
                        "Retrieved %d chars of resume context for resume %s.",
                        len(context),
                        resume_id,
                    )
                    return f"\nCandidate resume excerpt (most relevant):\n{context}", True
                logger.info(
                    "No indexed chunks for resume %s; using truncated resume text.",
                    resume_id,
                )

        if resume_text:
            return f"\nCandidate resume excerpt:\n{resume_text[:4000]}", False
        return "", False

    async def initial_questions(
        self,
        *,
        target_role: str | None,
        resume_text: str | None,
        resume_id: uuid.UUID | None = None,
    ) -> list[GeneratedQuestion]:
        role = target_role or "a general software engineering role"

        resume_context, used_rag = await self._resume_context(
            resume_text=resume_text,
            resume_id=resume_id,
            query=f"skills and experience relevant to {role}",
        )

        prompt = (
            f"Generate exactly 5 mock interview questions for {role}. "
            "Mix behavioral and technical questions. "
            'Respond as JSON: {"questions": [{"content": str, "question_type": '
            '"behavioral"|"technical"}]}.'
            f"{resume_context}"
        )
        payload = await self._client.generate_json(
            system_instruction=_SYSTEM, prompt=prompt
        )
        items = payload.get("questions", []) if isinstance(payload, dict) else []
        questions: list[GeneratedQuestion] = []
        for item in items:
            content = str(item.get("content", "")).strip()
            qtype = str(item.get("question_type", "behavioral")).strip().lower()
            if not content:
                continue
            if qtype not in _VALID_TYPES or qtype == "follow_up":
                qtype = "behavioral"
            questions.append(
                GeneratedQuestion(
                    content=content,
                    question_type=qtype,
                    metadata={"source": "gemini", "uses_rag": used_rag},
                )
            )
        if not questions:
            # Treat an empty model result as a failure so the factory falls back.
            from app.services.ai.gemini_client import GeminiError

            raise GeminiError("Gemini returned no usable questions.")
        return questions

    async def follow_up(
        self,
        *,
        question: str,
        answer: str,
        resume_text: str | None,
        resume_id: uuid.UUID | None = None,
    ) -> GeneratedQuestion | None:
        # Retrieval is keyed on the answer, not the role: the useful follow-up
        # is the one that probes a claim the candidate just made against what
        # the resume actually says.
        resume_context, used_rag = await self._resume_context(
            resume_text=resume_text,
            resume_id=resume_id,
            query=f"{question}\n{answer}",
        )

        prompt = (
            "Given this interview question and the candidate's answer, decide if a "
            "single probing follow-up question would add value. "
            "Prefer a follow-up that digs into a specific claim in the answer, "
            "using the resume excerpt below for concrete detail where relevant. "
            'Respond as JSON: {"ask_follow_up": bool, "content": str}. '
            f"\nQuestion: {question}\nAnswer: {answer}"
            f"{resume_context}"
        )
        payload = await self._client.generate_json(
            system_instruction=_SYSTEM, prompt=prompt
        )
        if not isinstance(payload, dict) or not payload.get("ask_follow_up"):
            return None
        content = str(payload.get("content", "")).strip()
        if not content:
            return None
        return GeneratedQuestion(
            content=content,
            question_type="follow_up",
            metadata={"source": "gemini", "uses_rag": used_rag},
        )
