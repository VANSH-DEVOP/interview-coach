"""Interview answer evaluation.

Produces an evaluation report payload from the question/answer transcript. Uses
Gemini when configured, with a deterministic heuristic fallback so completed
interviews always yield a populated report.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.services.ai.degradation import record_fallback


def _first(payload: dict[str, Any], keys: list[str], default: Any) -> Any:
    """Return the first present, non-null value among candidate keys.

    Models are inconsistent about field names (e.g. "weaknesses" vs
    "areas_for_improvement"); this tolerates the common variants.
    """
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return value
    return default


def _coerce_score(value: Any) -> float | None:
    """Clamp a model-supplied score into 0-10, or None if it isn't a number.

    Models return scores as ints, floats, and strings like "7/10" or "8.5".
    Anything unparseable becomes None rather than a misleading zero -- an
    absent score and a score of zero mean very different things to a reader.
    """
    if isinstance(value, str):
        value = value.split("/")[0].strip()
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, min(score, 10.0)), 2)


def _normalise_per_question(items: Any) -> list[dict[str, Any]]:
    """Coerce the model's per-question list into a stable shape for the UI."""
    if not isinstance(items, list):
        return []

    normalised: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        entry: dict[str, Any] = {
            "question": str(_first(item, ["question", "prompt"], "")).strip(),
            "feedback": str(_first(item, ["feedback", "comment", "notes"], "")).strip(),
            "score": _coerce_score(
                _first(item, ["score", "rating", "question_score"], None)
            ),
        }
        answered = _first(item, ["answered"], None)
        if answered is not None:
            entry["answered"] = bool(answered)
        normalised.append(entry)
    return normalised


def _as_str_list(value: Any) -> list[str]:
    """Coerce a model field into a clean list[str].

    Handles lists, a single string, or dicts (e.g. {"point": "..."}).
    """
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        items: list[str] = []
        for item in value:
            if isinstance(item, dict):
                # Pick the most descriptive string field if present.
                text = (
                    item.get("text")
                    or item.get("description")
                    or item.get("point")
                    or next((str(v) for v in item.values() if isinstance(v, str)), "")
                )
            else:
                text = str(item)
            text = text.strip()
            if text:
                items.append(text)
        return items
    return [str(value)]


@dataclass(frozen=True, slots=True)
class QAPair:
    question: str
    answer: str | None


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    overall_score: Decimal
    strengths: list[str]
    weaknesses: list[str]
    detailed_feedback: dict[str, Any] = field(default_factory=dict)


class Evaluator(ABC):
    @abstractmethod
    async def evaluate(
        self, *, target_role: str | None, transcript: list[QAPair]
    ) -> EvaluationResult: ...


class HeuristicEvaluator(Evaluator):
    """Deterministic, provider-free scoring based on answer coverage/length.

    Not a substitute for AI judgement, but gives users meaningful, non-empty
    feedback when no AI provider is configured.
    """

    async def evaluate(
        self, *, target_role: str | None, transcript: list[QAPair]
    ) -> EvaluationResult:
        total = len(transcript)
        answered = [qa for qa in transcript if qa.answer and qa.answer.strip()]
        answered_count = len(answered)

        if total == 0:
            return EvaluationResult(
                overall_score=Decimal("0.00"),
                strengths=[],
                weaknesses=["No questions were available to evaluate."],
                detailed_feedback={"per_question": []},
            )

        coverage = answered_count / total
        avg_words = (
            sum(len(qa.answer.split()) for qa in answered) / answered_count
            if answered_count
            else 0
        )
        # Coverage drives most of the score; answer depth gives a small bonus.
        depth_bonus = min(avg_words / 80.0, 1.0)  # ~80 words = full depth credit
        score = round((coverage * 0.7 + depth_bonus * 0.3) * 10, 2)

        strengths: list[str] = []
        weaknesses: list[str] = []
        if coverage == 1.0:
            strengths.append("Answered every question in the session.")
        elif coverage >= 0.5:
            strengths.append("Answered the majority of the questions.")
        else:
            weaknesses.append("Several questions were left unanswered.")
        if avg_words >= 60:
            strengths.append("Provided detailed, substantive responses.")
        elif answered_count and avg_words < 25:
            weaknesses.append("Responses were brief; add concrete examples and detail.")
        if not strengths:
            strengths.append("Engaged with the interview session.")

        per_question = []
        for qa in transcript:
            words = len(qa.answer.split()) if qa.answer else 0
            is_answered = bool(qa.answer and qa.answer.strip())
            per_question.append(
                {
                    "question": qa.question,
                    "answered": is_answered,
                    # Same 80-word full-credit scale as the overall depth bonus,
                    # so a per-question score is consistent with the total.
                    "score": round(min(words / 80.0, 1.0) * 10, 2) if is_answered else 0.0,
                    "feedback": (
                        "Good — consider quantifying impact with metrics."
                        if words >= 40
                        else "Expand with a specific example and the outcome."
                        if is_answered
                        else "Not answered."
                    ),
                }
            )

        return EvaluationResult(
            overall_score=Decimal(str(score)),
            strengths=strengths,
            weaknesses=weaknesses,
            detailed_feedback={
                "summary": (
                    f"Answered {answered_count} of {total} questions "
                    f"for {target_role or 'the target role'}."
                ),
                "recommendations": [
                    "Use the STAR method (Situation, Task, Action, Result).",
                    "Quantify achievements where possible.",
                    "Tie answers back to the target role's responsibilities.",
                ],
                "per_question": per_question,
            },
        )


class GeminiEvaluator(Evaluator):
    def __init__(self, client: Any) -> None:
        self._client = client

    async def evaluate(
        self, *, target_role: str | None, transcript: list[QAPair]
    ) -> EvaluationResult:
        lines = [
            f"Q{i}: {qa.question}\nA{i}: {qa.answer or '(no answer)'}"
            for i, qa in enumerate(transcript, start=1)
        ]
        prompt = (
            f"Evaluate this mock interview for {target_role or 'a software role'}. "
            "Score 0-10. Respond as JSON: "
            '{"overall_score": number, "summary": str, "strengths": [str], '
            '"weaknesses": [str], "recommendations": [str], "per_question": '
            '[{"question": str, "score": number, "feedback": str}]}. '
            '"summary" is a 2-3 sentence overall assessment of the candidate\'s '
            "performance, written directly to the candidate. "
            'Each "per_question" entry needs its own 0-10 "score" for that '
            "answer alone. Score an unanswered question 0."
            "\n\nTranscript:\n" + "\n\n".join(lines)
        )
        payload = await self._client.generate_json(
            system_instruction=(
                "You are a rigorous, fair interview evaluator. Respond with valid "
                "JSON only."
            ),
            prompt=prompt,
        )
        if not isinstance(payload, dict):
            raise ValueError("Gemini evaluation returned non-object JSON.")

        raw_score = float(_first(payload, ["overall_score", "score", "rating"], 0))
        score = Decimal(str(round(max(0.0, min(raw_score, 10.0)), 2)))

        strengths = _as_str_list(
            _first(payload, ["strengths", "positives", "pros"], [])
        )
        weaknesses = _as_str_list(
            _first(
                payload,
                [
                    "weaknesses",
                    "areas_for_improvement",
                    "areas_to_improve",
                    "improvements",
                    "areas_of_improvement",
                    "cons",
                ],
                [],
            )
        )
        recommendations = _as_str_list(
            _first(payload, ["recommendations", "suggestions", "advice"], [])
        )

        # The report UI renders detailed_feedback.summary as the headline. Model
        # key naming varies, and a model that omits it entirely would leave the
        # panel blank, so fall back to a synthesised line rather than nothing.
        summary = str(
            _first(payload, ["summary", "overall_feedback", "overall", "assessment"], "")
        ).strip()
        if not summary:
            summary = (
                f"Scored {score} out of 10 across {len(transcript)} "
                f"question{'s' if len(transcript) != 1 else ''} for "
                f"{target_role or 'the target role'}."
            )

        # A non-perfect score with no stated weaknesses is contradictory. Surface
        # the recommendations as improvement areas so the user always gets
        # actionable feedback when they didn't score a perfect 10.
        if not weaknesses and score < Decimal("10"):
            weaknesses = recommendations or [
                "Add more depth and concrete, quantified examples to your answers."
            ]

        return EvaluationResult(
            overall_score=score,
            strengths=strengths,
            weaknesses=weaknesses,
            detailed_feedback={
                "summary": summary,
                "recommendations": recommendations,
                "per_question": _normalise_per_question(payload.get("per_question")),
            },
        )


class FallbackEvaluator(Evaluator):
    def __init__(self, primary: Evaluator, fallback: Evaluator) -> None:
        self._primary = primary
        self._fallback = fallback

    async def evaluate(
        self, *, target_role: str | None, transcript: list[QAPair]
    ) -> EvaluationResult:
        try:
            return await self._primary.evaluate(
                target_role=target_role, transcript=transcript
            )
        except Exception as exc:  # noqa: BLE001 - any provider failure -> safe fallback
            record_fallback("evaluate", exc)
            return await self._fallback.evaluate(
                target_role=target_role, transcript=transcript
            )


def get_evaluator() -> Evaluator:
    from app.core.config import get_settings

    settings = get_settings()
    if settings.GEMINI_API_KEY:
        from app.services.ai.gemini_client import GeminiClient

        client = GeminiClient(settings.GEMINI_API_KEY, settings.GEMINI_MODEL)
        return FallbackEvaluator(GeminiEvaluator(client), HeuristicEvaluator())
    return HeuristicEvaluator()
