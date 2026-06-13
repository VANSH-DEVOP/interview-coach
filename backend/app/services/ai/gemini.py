"""Gemini-backed question generation.

Activated only when GEMINI_API_KEY is configured. On any API failure it raises
GeminiError; the factory wraps this generator so the static generator can take
over, guaranteeing the interview flow always works.
"""

from __future__ import annotations

from app.services.ai.base import GeneratedQuestion, QuestionGenerator
from app.services.ai.gemini_client import GeminiClient

_VALID_TYPES = {"behavioral", "technical", "follow_up"}

_SYSTEM = (
    "You are an expert technical interviewer. You generate concise, high-signal "
    "interview questions and always respond with valid JSON only."
)


class GeminiQuestionGenerator(QuestionGenerator):
    def __init__(self, client: GeminiClient) -> None:
        self._client = client

    async def initial_questions(
        self, *, target_role: str | None, resume_text: str | None
    ) -> list[GeneratedQuestion]:
        role = target_role or "a general software engineering role"
        resume_block = (
            f"\nCandidate resume excerpt:\n{resume_text[:4000]}" if resume_text else ""
        )
        prompt = (
            f"Generate exactly 5 mock interview questions for {role}. "
            "Mix behavioral and technical questions. "
            'Respond as JSON: {"questions": [{"content": str, "question_type": '
            '"behavioral"|"technical"}]}.'
            f"{resume_block}"
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
                    metadata={"source": "gemini"},
                )
            )
        if not questions:
            # Treat an empty model result as a failure so the factory falls back.
            from app.services.ai.gemini_client import GeminiError

            raise GeminiError("Gemini returned no usable questions.")
        return questions

    async def follow_up(
        self, *, question: str, answer: str, resume_text: str | None
    ) -> GeneratedQuestion | None:
        prompt = (
            "Given this interview question and the candidate's answer, decide if a "
            "single probing follow-up question would add value. "
            'Respond as JSON: {"ask_follow_up": bool, "content": str}. '
            f"\nQuestion: {question}\nAnswer: {answer}"
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
            content=content, question_type="follow_up", metadata={"source": "gemini"}
        )
