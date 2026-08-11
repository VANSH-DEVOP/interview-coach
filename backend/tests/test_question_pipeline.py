"""Extract -> generate -> critique -> refine.

One model call used to produce the interview and whatever came back *was* the
interview: three questions when five were asked for meant a three-question
interview, and nothing said so.

The quota is the design constraint. Twenty provider requests per day for the
whole account means extraction and critique have to be free, and refinement has
to be conditional -- a three-call chain takes the deployment from roughly six
interviews a day to two. So the tests here care as much about *how many calls
were made* as about the questions.
"""

import pytest

from app.services.ai.base import GeneratedQuestion, InterviewSpec
from app.services.ai.gemini import GeminiQuestionGenerator
from app.services.ai.pipeline import Critique, critique, extract_skills, trim_to_count

RESUME = """\
Rae Sandoval
Data Engineer

EXPERIENCE
Senior Data Engineer, Cartwheel (2021-2026)
Owned the warehouse ingestion path end to end.

SKILLS
Languages: Python, Go
Data: Kafka, Airflow
Practices: trunk-based development

CERTIFICATIONS
Certified Kubernetes Administrator, 2023
"""


def _q(content: str, qtype: str = "technical") -> GeneratedQuestion:
    return GeneratedQuestion(content=content, question_type=qtype, metadata={})


class _ScriptedClient:
    """Returns a queued payload per call and counts how many were made."""

    def __init__(self, *payloads) -> None:
        self.payloads = list(payloads)
        self.prompts: list[str] = []

    async def generate_json(self, *, system_instruction: str, prompt: str):
        self.prompts.append(prompt)
        return self.payloads.pop(0) if self.payloads else {"questions": []}

    @property
    def calls(self) -> int:
        return len(self.prompts)


def _payload(*contents, qtype="technical"):
    return {"questions": [{"content": c, "question_type": qtype} for c in contents]}


# -- Extraction ----------------------------------------------------------------


def test_skills_come_from_the_labelled_sections():
    skills = extract_skills(RESUME)

    assert "Python" in skills and "Kafka" in skills and "Airflow" in skills


def test_the_category_label_is_not_a_skill():
    """"Languages: Python, Go" claims Python and Go, not "Languages"."""
    assert "Languages" not in extract_skills(RESUME)
    assert "Data" not in extract_skills(RESUME)


def test_prose_sections_are_not_mined_for_skills():
    """Comma-splitting a sentence produces fragments that read like skills and
    are not. A question about something the candidate never claimed is worse
    than one question fewer."""
    skills = extract_skills(RESUME)

    assert not any("warehouse ingestion" in skill.lower() for skill in skills)


def test_a_year_beside_a_certification_is_not_a_skill():
    assert "2023" not in extract_skills(RESUME)


def test_no_resume_means_no_skills():
    assert extract_skills(None) == []
    assert extract_skills("") == []


def test_a_resume_with_no_skills_section_yields_nothing_rather_than_guessing():
    assert extract_skills("EXPERIENCE\nDid things with computers.\n") == []


# -- Critique ------------------------------------------------------------------


def test_the_wrong_count_is_a_problem():
    """The defect this part exists for: asked for five, given three, and the
    candidate sat a three-question interview."""
    problems = critique([_q("One"), _q("Two")], InterviewSpec(question_count=5), [])

    assert problems
    assert "exactly 5" in problems.instruction


def test_duplicate_questions_are_a_problem():
    problems = critique(
        [_q("Tell me about Kafka."), _q("tell me about kafka")],
        InterviewSpec(question_count=2),
        [],
    )

    assert "distinct" in problems.instruction


def test_the_requested_type_is_enforced():
    problems = critique(
        [_q("Describe a conflict.", "technical")],
        InterviewSpec(question_count=1, interview_type="behavioral"),
        [],
    )

    assert "behavioral questions only" in problems.instruction


def test_a_mixed_interview_needs_both_kinds():
    problems = critique(
        [_q("A", "technical"), _q("B", "technical")],
        InterviewSpec(question_count=2, interview_type="mixed"),
        [],
    )

    assert "Mix behavioral and technical" in problems.instruction


def test_questions_that_touch_none_of_the_resume_are_a_problem():
    """A set mentioning nothing the candidate claims is the generic interview
    the fallback would have produced, at the price of a provider call."""
    problems = critique(
        [_q("What is a linked list?"), _q("What is REST?")],
        InterviewSpec(question_count=2),
        ["Kafka", "Airflow"],
    )

    assert "own experience" in problems.instruction


def test_a_grounded_set_passes():
    problems = critique(
        [_q("How did you tune Kafka consumers?"), _q("Describe an Airflow DAG you own.")],
        # Explicitly technical: the default spec is "mixed", which would
        # correctly complain that two technical questions are not a mix.
        InterviewSpec(question_count=2, interview_type="technical"),
        ["Kafka", "Airflow"],
    )

    assert not problems


def test_grounding_is_not_checked_when_there_are_no_skills():
    """A resume with no SKILLS section must not fail every set forever."""
    assert not critique([_q("Anything")], InterviewSpec(question_count=1), [])


def test_trimming_never_pads():
    """A placeholder question is worse than a short set."""
    assert len(trim_to_count([_q("A")], 5)) == 1
    assert len(trim_to_count([_q("A"), _q("B"), _q("C")], 2)) == 2


# -- The chain, and what it costs ----------------------------------------------


async def test_a_good_set_costs_exactly_one_call():
    """The common case must not get dearer. Twenty requests a day for the whole
    account is the budget the whole chain is designed around."""
    client = _ScriptedClient(_payload("Tune Kafka consumers?", "Describe an Airflow DAG?"))

    questions = await GeminiQuestionGenerator(client).initial_questions(
        target_role="Data Engineer",
        resume_text=RESUME,
        spec=InterviewSpec(question_count=2, interview_type="technical"),
    )

    assert client.calls == 1
    assert len(questions) == 2


async def test_the_extracted_skills_reach_the_prompt():
    client = _ScriptedClient(_payload("Tune Kafka?", "Airflow?"))

    await GeminiQuestionGenerator(client).initial_questions(
        target_role="Data Engineer",
        resume_text=RESUME,
        spec=InterviewSpec(question_count=2, interview_type="technical"),
    )

    assert "Kafka" in client.prompts[0]
    assert "Probe these technologies" in client.prompts[0]


async def test_a_short_set_triggers_exactly_one_corrective_call():
    client = _ScriptedClient(
        _payload("Only Kafka question"),                       # asked for 3
        _payload("Kafka one", "Airflow two", "Python three"),  # corrected
    )

    questions = await GeminiQuestionGenerator(client).initial_questions(
        target_role="Data Engineer",
        resume_text=RESUME,
        spec=InterviewSpec(question_count=3, interview_type="technical"),
    )

    assert client.calls == 2
    assert len(questions) == 3
    assert "Fix exactly this" in client.prompts[1]
    assert "exactly 3 questions" in client.prompts[1]


async def test_refinement_is_not_retried_a_second_time():
    """A loop against a model that keeps missing the brief would spend a day's
    quota on one interview."""
    client = _ScriptedClient(_payload("One"), _payload("Still one"))

    await GeminiQuestionGenerator(client).initial_questions(
        target_role="Data Engineer",
        resume_text=RESUME,
        spec=InterviewSpec(question_count=4, interview_type="technical"),
    )

    assert client.calls == 2


async def test_a_refinement_that_is_no_better_is_discarded():
    """Fixing the count while introducing duplicates is not an improvement, and
    there is no reason to trust the second answer blindly."""
    client = _ScriptedClient(
        _payload("Kafka one", "Airflow two"),          # 2 of 3: one problem
        _payload("Same", "Same", "Same"),              # 3, but all duplicates
    )

    questions = await GeminiQuestionGenerator(client).initial_questions(
        target_role="Data Engineer",
        resume_text=RESUME,
        spec=InterviewSpec(question_count=3, interview_type="technical"),
    )

    assert [q.content for q in questions] == ["Kafka one", "Airflow two"]


async def test_a_failed_refinement_keeps_the_original_set():
    """Raising here would let the factory fall back to the static generator,
    throwing away a merely imperfect set in favour of a generic one."""

    class _FailsOnRefine(_ScriptedClient):
        async def generate_json(self, *, system_instruction, prompt):
            if self.prompts:
                self.prompts.append(prompt)
                raise RuntimeError("provider down")
            return await super().generate_json(
                system_instruction=system_instruction, prompt=prompt
            )

    client = _FailsOnRefine(_payload("Kafka one", "Airflow two"))

    questions = await GeminiQuestionGenerator(client).initial_questions(
        target_role="Data Engineer",
        resume_text=RESUME,
        spec=InterviewSpec(question_count=5, interview_type="technical"),
    )

    assert [q.content for q in questions] == ["Kafka one", "Airflow two"]


async def test_extra_questions_are_trimmed_without_a_call():
    """Too many is fixable for free; too few is not."""
    client = _ScriptedClient(_payload("Kafka", "Airflow", "Python", "Go"))

    questions = await GeminiQuestionGenerator(client).initial_questions(
        target_role="Data Engineer",
        resume_text=RESUME,
        spec=InterviewSpec(question_count=4, interview_type="technical"),
    )

    assert client.calls == 1
    assert len(questions) == 4


@pytest.mark.parametrize("problems", [Critique([]), Critique(["a"])])
def test_a_critique_is_falsy_only_when_there_is_nothing_to_fix(problems):
    assert bool(problems) == bool(problems.problems)
