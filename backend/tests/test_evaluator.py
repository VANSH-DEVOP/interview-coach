"""Unit tests for interview answer evaluation.

Covers the deterministic HeuristicEvaluator, the GeminiEvaluator JSON parsing
(with a fake client, no network), and the FallbackEvaluator's safety net.
"""

from decimal import Decimal

import pytest

from app.services.ai.evaluator import (
    EvaluationResult,
    Evaluator,
    FallbackEvaluator,
    GeminiEvaluator,
    HeuristicEvaluator,
    QAPair,
)


class _FakeClient:
    """Stand-in for GeminiClient: returns or raises a preconfigured value."""

    def __init__(self, *, payload=None, error: Exception | None = None) -> None:
        self._payload = payload
        self._error = error
        self.calls = 0

    async def generate_json(self, *, system_instruction: str, prompt: str):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._payload


class _BoomEvaluator(Evaluator):
    async def evaluate(self, *, target_role, transcript):
        raise RuntimeError("primary failed")


# -- HeuristicEvaluator -------------------------------------------------------


async def test_heuristic_empty_transcript_scores_zero():
    result = await HeuristicEvaluator().evaluate(target_role="Backend", transcript=[])
    assert result.overall_score == Decimal("0.00")
    assert result.strengths == []
    assert result.weaknesses  # has at least one note
    assert result.detailed_feedback["per_question"] == []


async def test_heuristic_full_coverage_and_depth_scores_high():
    long_answer = " ".join(["word"] * 100)
    transcript = [
        QAPair(question="Q1", answer=long_answer),
        QAPair(question="Q2", answer=long_answer),
    ]
    result = await HeuristicEvaluator().evaluate(
        target_role="Backend Engineer", transcript=transcript
    )
    # coverage 1.0 (0.7) + full depth bonus (0.3) -> 10.0
    assert result.overall_score == Decimal("10.0")
    assert "Answered every question in the session." in result.strengths
    assert "Provided detailed, substantive responses." in result.strengths
    assert len(result.detailed_feedback["per_question"]) == 2


async def test_heuristic_partial_coverage_flags_unanswered():
    transcript = [
        QAPair(question="Q1", answer="A short answer here."),
        QAPair(question="Q2", answer=None),
        QAPair(question="Q3", answer="   "),  # whitespace counts as unanswered
    ]
    result = await HeuristicEvaluator().evaluate(target_role=None, transcript=transcript)
    # Only 1 of 3 answered -> coverage < 0.5
    assert "Several questions were left unanswered." in result.weaknesses
    assert Decimal("0") < result.overall_score < Decimal("10")
    per_q = result.detailed_feedback["per_question"]
    assert per_q[1]["answered"] is False
    assert per_q[1]["feedback"] == "Not answered."


async def test_heuristic_brief_answers_flagged():
    transcript = [QAPair(question="Q1", answer="Too short.")]
    result = await HeuristicEvaluator().evaluate(target_role="Role", transcript=transcript)
    assert any("brief" in w.lower() for w in result.weaknesses)


# -- GeminiEvaluator ----------------------------------------------------------


async def test_gemini_evaluator_parses_payload():
    client = _FakeClient(
        payload={
            "overall_score": 7.5,
            "strengths": ["Clear communication"],
            "weaknesses": ["Lacked metrics"],
            "recommendations": ["Quantify results"],
            "per_question": [{"question": "Q1", "feedback": "Solid"}],
        }
    )
    result = await GeminiEvaluator(client).evaluate(
        target_role="Backend", transcript=[QAPair(question="Q1", answer="A1")]
    )
    assert result.overall_score == Decimal("7.5")
    assert result.strengths == ["Clear communication"]
    assert result.weaknesses == ["Lacked metrics"]
    assert result.detailed_feedback["recommendations"] == ["Quantify results"]
    assert result.detailed_feedback["per_question"][0]["feedback"] == "Solid"
    assert client.calls == 1


async def test_gemini_evaluator_emits_summary():
    # report-view.tsx renders detailed_feedback.summary as the headline; the
    # Gemini path used to omit the key entirely, leaving the panel blank.
    client = _FakeClient(
        payload={
            "overall_score": 7.5,
            "summary": "A solid interview with room for more concrete detail.",
            "strengths": ["Clear communication"],
            "weaknesses": ["Lacked metrics"],
        }
    )
    result = await GeminiEvaluator(client).evaluate(
        target_role="Backend", transcript=[QAPair(question="Q1", answer="A1")]
    )
    assert (
        result.detailed_feedback["summary"]
        == "A solid interview with room for more concrete detail."
    )


async def test_gemini_evaluator_reads_summary_key_aliases():
    client = _FakeClient(payload={"overall_score": 6, "overall_feedback": "Decent showing."})
    result = await GeminiEvaluator(client).evaluate(
        target_role="Backend", transcript=[QAPair(question="Q1", answer="A1")]
    )
    assert result.detailed_feedback["summary"] == "Decent showing."


async def test_gemini_evaluator_synthesises_missing_summary():
    # A model that omits summary must not produce an empty headline.
    client = _FakeClient(payload={"overall_score": 6, "strengths": ["Good energy"]})
    result = await GeminiEvaluator(client).evaluate(
        target_role="Backend", transcript=[QAPair(question="Q1", answer="A1")]
    )
    summary = result.detailed_feedback["summary"]
    assert summary
    assert "6" in summary and "Backend" in summary


async def test_gemini_evaluator_parses_per_question_scores():
    client = _FakeClient(
        payload={
            "overall_score": 7,
            "per_question": [
                {"question": "Q1", "score": 8.5, "feedback": "Strong"},
                {"question": "Q2", "score": 3, "feedback": "Thin"},
            ],
        }
    )
    result = await GeminiEvaluator(client).evaluate(target_role=None, transcript=[])
    per_q = result.detailed_feedback["per_question"]
    assert [p["score"] for p in per_q] == [8.5, 3.0]


@pytest.mark.parametrize(
    "raw,expected",
    [
        (8, 8.0),
        ("7", 7.0),
        ("8.5/10", 8.5),  # models like writing "8.5/10"
        (42, 10.0),  # clamped
        (-3, 0.0),  # clamped
        (None, None),  # absent stays absent, not zero
        ("great", None),  # unparseable stays absent
    ],
)
async def test_gemini_evaluator_coerces_per_question_scores(raw, expected):
    client = _FakeClient(
        payload={"per_question": [{"question": "Q", "score": raw, "feedback": "f"}]}
    )
    result = await GeminiEvaluator(client).evaluate(target_role=None, transcript=[])
    assert result.detailed_feedback["per_question"][0]["score"] == expected


async def test_gemini_evaluator_reads_per_question_score_aliases():
    client = _FakeClient(
        payload={"per_question": [{"question": "Q", "rating": 6, "comment": "ok"}]}
    )
    result = await GeminiEvaluator(client).evaluate(target_role=None, transcript=[])
    entry = result.detailed_feedback["per_question"][0]
    assert entry["score"] == 6.0
    assert entry["feedback"] == "ok"


async def test_gemini_evaluator_survives_a_malformed_per_question_list():
    client = _FakeClient(payload={"per_question": "not a list"})
    result = await GeminiEvaluator(client).evaluate(target_role=None, transcript=[])
    assert result.detailed_feedback["per_question"] == []


async def test_heuristic_scores_each_question():
    transcript = [
        QAPair(question="Q1", answer=" ".join(["word"] * 80)),  # full depth credit
        QAPair(question="Q2", answer="Short."),
        QAPair(question="Q3", answer=None),
    ]
    result = await HeuristicEvaluator().evaluate(target_role="Role", transcript=transcript)
    scores = [p["score"] for p in result.detailed_feedback["per_question"]]

    assert scores[0] == 10.0
    assert 0 < scores[1] < 10
    assert scores[2] == 0.0  # unanswered is a real zero, not a missing score


async def test_gemini_evaluator_clamps_score_to_range():
    client = _FakeClient(payload={"overall_score": 42})
    result = await GeminiEvaluator(client).evaluate(target_role=None, transcript=[])
    assert result.overall_score == Decimal("10.0")


async def test_gemini_evaluator_reads_weakness_key_aliases():
    # Model uses "areas_for_improvement" instead of "weaknesses".
    client = _FakeClient(
        payload={
            "score": 8.7,
            "positives": ["Strong system design"],
            "areas_for_improvement": ["Be more concise", "Add metrics"],
            "suggestions": ["Practice the STAR method"],
        }
    )
    result = await GeminiEvaluator(client).evaluate(
        target_role="Backend", transcript=[QAPair(question="Q1", answer="A1")]
    )
    assert result.overall_score == Decimal("8.7")
    assert result.strengths == ["Strong system design"]
    assert result.weaknesses == ["Be more concise", "Add metrics"]
    assert result.detailed_feedback["recommendations"] == ["Practice the STAR method"]


async def test_gemini_evaluator_non_perfect_score_always_has_weaknesses():
    # Regression: 8.7/10 with an empty weaknesses list must not render blank.
    client = _FakeClient(
        payload={
            "overall_score": 8.7,
            "strengths": ["Great answers"],
            "weaknesses": [],
            "recommendations": ["Quantify your impact"],
        }
    )
    result = await GeminiEvaluator(client).evaluate(
        target_role="Backend", transcript=[QAPair(question="Q1", answer="A1")]
    )
    assert result.overall_score == Decimal("8.7")
    assert result.weaknesses == ["Quantify your impact"]


async def test_gemini_evaluator_perfect_score_may_have_no_weaknesses():
    client = _FakeClient(
        payload={"overall_score": 10, "strengths": ["Flawless"], "weaknesses": []}
    )
    result = await GeminiEvaluator(client).evaluate(
        target_role="Backend", transcript=[QAPair(question="Q1", answer="A1")]
    )
    assert result.overall_score == Decimal("10.0")
    assert result.weaknesses == []


async def test_gemini_evaluator_coerces_dict_weakness_items():
    client = _FakeClient(
        payload={
            "overall_score": 6,
            "weaknesses": [{"description": "Too vague"}, {"point": "No examples"}],
        }
    )
    result = await GeminiEvaluator(client).evaluate(
        target_role=None, transcript=[QAPair(question="Q1", answer="A1")]
    )
    assert result.weaknesses == ["Too vague", "No examples"]


async def test_gemini_evaluator_rejects_non_object_payload():
    client = _FakeClient(payload=["not", "an", "object"])
    with pytest.raises(ValueError):
        await GeminiEvaluator(client).evaluate(target_role=None, transcript=[])


# -- FallbackEvaluator --------------------------------------------------------


async def test_fallback_uses_primary_when_it_succeeds():
    expected = EvaluationResult(
        overall_score=Decimal("9.0"), strengths=["s"], weaknesses=[]
    )

    class _Primary(Evaluator):
        async def evaluate(self, *, target_role, transcript):
            return expected

    evaluator = FallbackEvaluator(_Primary(), HeuristicEvaluator())
    result = await evaluator.evaluate(target_role="Role", transcript=[])
    assert result is expected


async def test_fallback_recovers_when_primary_raises():
    evaluator = FallbackEvaluator(_BoomEvaluator(), HeuristicEvaluator())
    transcript = [QAPair(question="Q1", answer="An answer.")]
    result = await evaluator.evaluate(target_role="Role", transcript=transcript)
    # Heuristic fallback produced a real, populated result.
    assert isinstance(result, EvaluationResult)
    assert result.detailed_feedback["per_question"]


async def test_fallback_evaluator_recovers_from_gemini_client_error():
    bad_client = _FakeClient(error=RuntimeError("network down"))
    evaluator = FallbackEvaluator(GeminiEvaluator(bad_client), HeuristicEvaluator())
    result = await evaluator.evaluate(
        target_role="Backend", transcript=[QAPair(question="Q1", answer="A1")]
    )
    assert isinstance(result.overall_score, Decimal)
    assert bad_client.calls == 1
