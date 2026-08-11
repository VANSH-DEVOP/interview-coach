"""Gemini-backed question generation.

Activated only when GEMINI_API_KEY is configured. On any API failure it raises
GeminiError; the factory wraps this generator so the static generator can take
over, guaranteeing the interview flow always works.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from app.services.ai import retrieval_metrics
from app.services.ai.base import GeneratedQuestion, InterviewSpec, QuestionGenerator
from app.services.ai.gemini_client import GeminiClient
from app.services.ai.pipeline import Critique, critique, extract_skills, trim_to_count
from app.services.ai.query import rewrite_for_follow_up, rewrite_for_role
from app.services.ai.untrusted import Fence

if TYPE_CHECKING:
    from app.services.ai.masking import Redactor
    from app.services.ai.retrieval import Retriever
    from app.services.ai.retrieval_metrics import Purpose

logger = logging.getLogger(__name__)

_VALID_TYPES = {"behavioral", "technical", "follow_up"}

# What each interview type asks the model for. System-design questions are
# stored as "technical": the question_type enum is a storage concern with its
# own Postgres type, and widening it is not needed to shape the interview.
_TYPE_INSTRUCTIONS = {
    "behavioral": (
        "Ask only behavioral questions about past experience, collaboration, "
        "conflict, and ownership. Set question_type to \"behavioral\"."
    ),
    "technical": (
        "Ask only hands-on technical questions about languages, frameworks, "
        "debugging, and code-level trade-offs. Set question_type to \"technical\"."
    ),
    "system_design": (
        "Ask only system design questions about architecture, scaling, data "
        "modelling, and trade-offs at scale. Set question_type to \"technical\"."
    ),
    "mixed": "Mix behavioral and technical questions.",
}

_DIFFICULTY_INSTRUCTIONS = {
    "junior": (
        "Target a junior engineer (0-2 years): fundamentals, guided scope, "
        "and questions answerable without deep production experience."
    ),
    "mid": "Target a mid-level engineer (2-5 years) with production experience.",
    "senior": (
        "Target a senior engineer (5+ years): ambiguity, trade-offs, technical "
        "leadership, and decisions with lasting consequences."
    ),
}

_SYSTEM = (
    "You are an expert technical interviewer. You generate concise, high-signal "
    "interview questions and always respond with valid JSON only."
)


class GeminiQuestionGenerator(QuestionGenerator):
    def __init__(
        self,
        client: GeminiClient,
        retriever: "Retriever | None" = None,
        redactor: "Redactor | None" = None,
    ) -> None:
        self._client = client
        self._retriever = retriever
        # Only for retrieval: the client redacts its own prompts. Retrieval
        # embeds the query on a different HTTP call that the client never sees.
        self._redactor = redactor

    async def _resume_context(
        self,
        *,
        resume_text: str | None,
        resume_id: uuid.UUID | None,
        query: str,
        purpose: "Purpose" = "initial_questions",
        fence: Fence | None = None,
    ) -> tuple[str, bool]:
        """Build the resume section of a prompt, preferring RAG over truncation.

        Returns (prompt_fragment, used_rag). Falls back to truncated raw text
        whenever retrieval is unavailable, fails, *or returns nothing* -- the
        last case matters for resumes uploaded before the vector index worked,
        which are in the database but absent from Chroma. Dropping the resume
        entirely there would silently de-personalise the interview.
        """
        reason = "no_resume_text"
        if self._retriever and resume_id and resume_text:
            try:
                context = await self._retriever.retrieve_context(
                    resume_id, query, top_k=5, redactor=self._redactor, purpose=purpose
                )
            except Exception as e:
                reason = "retrieval_failed"
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
                    excerpt = fence.wrap(context) if fence else context
                    return f"\nCandidate resume excerpt (most relevant):\n{excerpt}", True
                reason = "not_indexed"
                logger.info(
                    "No indexed chunks for resume %s; using truncated resume text.",
                    resume_id,
                )
        elif resume_text:
            # Retrieval was never asked. Either there is no RAG service at all
            # (no key, or Chroma disabled itself) or the session has no resume
            # attached. Both produce the same de-personalised prompt as a failed
            # retrieval, which is why they are counted together.
            reason = "retrieval_unavailable" if resume_id else "no_resume_attached"

        if resume_text:
            # Counted here rather than in RAGService: this is the only place
            # that sees every route to a truncated-resume prompt, including the
            # ones where retrieval was never called.
            retrieval_metrics.record_full_text_fallback(purpose=purpose, reason=reason)
            excerpt = resume_text[:4000]
            if fence:
                excerpt = fence.wrap(excerpt)
            return f"\nCandidate resume excerpt:\n{excerpt}", False
        return "", False

    async def initial_questions(
        self,
        *,
        target_role: str | None,
        resume_text: str | None,
        resume_id: uuid.UUID | None = None,
        spec: InterviewSpec | None = None,
    ) -> list[GeneratedQuestion]:
        role = target_role or "a general software engineering role"
        spec = spec or InterviewSpec()

        # The resume is uploaded by the candidate, so it is untrusted input to
        # a prompt whose output shapes their own interview.
        fence = Fence()
        resume_context, used_rag = await self._resume_context(
            resume_text=resume_text,
            resume_id=resume_id,
            query=rewrite_for_role(role),
            purpose="initial_questions",
            fence=fence,
        )

        # What the candidate says they know, read straight out of the resume's
        # own SKILLS block. Free -- part 2's chunker already labelled it -- and
        # it cannot invent a technology they never claimed, which a model
        # asked to "extract skills" can.
        skills = extract_skills(resume_text)

        prompt = self._initial_prompt(
            role=role,
            spec=spec,
            skills=skills,
            resume_context=resume_context,
            fence=fence,
        )
        payload = await self._client.generate_json(
            system_instruction=_SYSTEM, prompt=prompt
        )
        questions = self._parse_questions(payload, used_rag=used_rag)
        if not questions:
            # Treat an empty model result as a failure so the factory falls back.
            from app.services.ai.gemini_client import GeminiError

            raise GeminiError("Gemini returned no usable questions.")

        # Check what came back against what was asked for. Before this, a model
        # that returned three questions when five were requested produced a
        # three-question interview and nothing anywhere said so.
        problems = critique(questions, spec, skills)
        if problems:
            questions = await self._refine(
                questions, problems=problems, prompt=prompt, spec=spec,
                skills=skills, used_rag=used_rag,
            )

        return trim_to_count(questions, spec.question_count)

    def _initial_prompt(
        self,
        *,
        role: str,
        spec: InterviewSpec,
        skills: list[str],
        resume_context: str,
        fence: Fence,
    ) -> str:
        type_instruction = _TYPE_INSTRUCTIONS.get(
            spec.interview_type, _TYPE_INSTRUCTIONS["mixed"]
        )
        difficulty_instruction = _DIFFICULTY_INSTRUCTIONS.get(
            spec.difficulty, _DIFFICULTY_INSTRUCTIONS["mid"]
        )
        # Naming the technologies is what stops the model asking a generic
        # question about the role when the retrieved context mentions something
        # far more specific.
        skills_instruction = (
            "Probe these technologies the candidate lists, where they fit: "
            + ", ".join(skills)
            + ". "
            if skills
            else ""
        )
        return (
            f"Generate exactly {spec.question_count} mock interview questions "
            f"for {role}. "
            f"{type_instruction} "
            f"{difficulty_instruction} "
            f"{skills_instruction}"
            'Respond as JSON: {"questions": [{"content": str, "question_type": '
            '"behavioral"|"technical"}]}.'
            + (f"\n\n{fence.instruction}" if resume_context else "")
            + f"{resume_context}"
        )

    def _parse_questions(
        self, payload: object, *, used_rag: bool
    ) -> list[GeneratedQuestion]:
        items = payload.get("questions", []) if isinstance(payload, dict) else []
        questions: list[GeneratedQuestion] = []
        for item in items:
            if not isinstance(item, dict):
                continue
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
        return questions

    async def _refine(
        self,
        questions: list[GeneratedQuestion],
        *,
        problems: Critique,
        prompt: str,
        spec: InterviewSpec,
        skills: list[str],
        used_rag: bool,
    ) -> list[GeneratedQuestion]:
        """One corrective call, and only when the critique found something.

        Three rules, all about not making things worse:

        - **At most one retry.** A loop against a model that keeps missing the
          brief would eat a day's quota on a single interview.
        - **The result is re-critiqued and kept only if it is better.** A
          refinement that fixes the count while introducing duplicates is not
          an improvement, and there is no reason to trust it blindly.
        - **A failed refinement returns the original.** Raising here would let
          the factory fall back to the static generator, throwing away a set
          that was merely imperfect in favour of one that is generic.
        """
        logger.info("Refining generated questions: %s", problems.instruction)
        existing = "\n".join(
            f"- [{question.question_type}] {question.content}" for question in questions
        )
        try:
            payload = await self._client.generate_json(
                system_instruction=_SYSTEM,
                prompt=(
                    f"{prompt}\n\nYour previous answer did not meet the brief.\n"
                    f"You returned:\n{existing}\n\n"
                    f"Fix exactly this: {problems.instruction} "
                    "Return the full corrected set in the same JSON format."
                ),
            )
            refined = self._parse_questions(payload, used_rag=used_rag)
        except Exception as exc:  # noqa: BLE001 - a bad refinement is not a failure
            logger.warning("Refinement call failed; keeping the original set: %s", exc)
            return questions

        if not refined:
            return questions
        if len(critique(refined, spec, skills).problems) < len(problems.problems):
            return refined
        logger.info("Refinement did not improve the set; keeping the original.")
        return questions

        # "exactly N" is a request, not a guarantee. Trim overruns so the user
        # gets the length they chose; a short reply is left as-is rather than
        # discarded, since fewer good questions beats falling back to static.
        if len(questions) > spec.question_count:
            logger.info(
                "Gemini returned %d questions for a %d-question interview; trimming.",
                len(questions),
                spec.question_count,
            )
            questions = questions[: spec.question_count]
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
        fence = Fence()
        resume_context, used_rag = await self._resume_context(
            resume_text=resume_text,
            resume_id=resume_id,
            query=rewrite_for_follow_up(question, answer),
            purpose="follow_up",
            fence=fence,
        )

        prompt = (
            "Given this interview question and the candidate's answer, decide if a "
            "single probing follow-up question would add value. "
            "Prefer a follow-up that digs into a specific claim in the answer, "
            "using the resume excerpt below for concrete detail where relevant. "
            'Respond as JSON: {"ask_follow_up": bool, "content": str}. '
            f"\n\n{fence.instruction}"
            f"\nQuestion: {question}\nAnswer: {fence.wrap(answer)}"
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
