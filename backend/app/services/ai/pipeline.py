"""Multi-step question generation: extract, generate, critique, refine.

One model call used to produce the interview, and whatever came back was the
interview. If it returned three questions when five were asked for, the
candidate sat a three-question interview and nothing said so. If every question
was generic, that was the interview too.

The chain is extract -> generate -> critique -> refine, and **only the
generate step always costs a provider call**. That matters more here than
anywhere: the free tier allows twenty requests per day for the whole account,
so the obvious implementation -- a model call to pull out skills, a model call
to generate, a model call to critique -- would take the deployment from roughly
six interviews a day to two. Extraction and critique are therefore ordinary
Python, and refine fires only when the critique has something to say.

Extraction is free because part 2 already did the work: the chunker labels a
resume's own sections, so the technologies a candidate claims are sitting in
the SKILLS block waiting to be read rather than inferred.

Critique is deliberately about things a rule can settle -- the count,
duplicates, the requested type mix, whether any question touches the resume at
all. It does not try to judge whether a question is *good*; that needs a model,
and a model call per interview to grade the interview is the trade this module
exists to avoid.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services.ai.base import GeneratedQuestion, InterviewSpec
from app.services.ai.rag import ResumeChunker

# Sections whose contents are lists of things the candidate claims to know.
# Deliberately narrow: PROJECTS and EXPERIENCE are prose, and comma-splitting
# prose produces fragments that read like skills and are not. A question about
# a technology the candidate never claimed is worse than one fewer question.
_SKILL_SECTIONS = frozenset(
    {"skills", "technical skills", "technologies", "competencies", "certifications"}
)

# A skill is a short noun phrase. Anything longer is a sentence that happened to
# follow a comma; anything shorter is punctuation or a stray initial.
_MIN_SKILL_CHARS = 2
_MAX_SKILL_CHARS = 40
_MAX_SKILL_WORDS = 4

# Enough to steer the questions, few enough to leave prompt budget for the
# retrieved context that actually carries the detail.
_MAX_SKILLS = 12

_WORDS = re.compile(r"[a-z0-9]+")


def extract_skills(resume_text: str | None) -> list[str]:
    """The technologies a resume explicitly claims, in document order.

    Reads the labelled sections rather than asking a model, which costs nothing
    and cannot hallucinate a skill the candidate never wrote down. Works when
    retrieval is switched off, since it needs only the parsed text.
    """
    if not resume_text:
        return []

    skills: list[str] = []
    seen: set[str] = set()
    for chunk in ResumeChunker().chunk(resume_text):
        if not chunk.section or chunk.section.lower() not in _SKILL_SECTIONS:
            continue
        for line in chunk.content.splitlines():
            # "Languages: Python, Go, SQL" -- the label is a category, not a
            # skill, so only what follows the colon is taken.
            _, _, listed = line.rpartition(":")
            for candidate in (listed or line).split(","):
                skill = candidate.strip(" .;•-\t")
                if not _is_skill(skill):
                    continue
                key = skill.lower()
                if key in seen:
                    continue
                seen.add(key)
                skills.append(skill)
    return skills[:_MAX_SKILLS]


def _is_skill(text: str) -> bool:
    if not (_MIN_SKILL_CHARS <= len(text) <= _MAX_SKILL_CHARS):
        return False
    if len(text.split()) > _MAX_SKILL_WORDS:
        return False
    # "2023" from "Certified Kubernetes Administrator, 2023".
    return any(character.isalpha() for character in text)


@dataclass(frozen=True)
class Critique:
    """What is wrong with a generated question set, in words a prompt can use."""

    problems: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        """True when there is something to fix."""
        return bool(self.problems)

    @property
    def instruction(self) -> str:
        return " ".join(self.problems)


def _normalised(question: str) -> str:
    return " ".join(_WORDS.findall(question.lower()))


def critique(
    questions: list[GeneratedQuestion], spec: InterviewSpec, skills: list[str]
) -> Critique:
    """Check a generated set against what was asked for.

    Only failures a rule can be sure about. "This question is bland" is a
    judgement that needs a model, and spending a provider call per interview to
    grade the interview is what this module exists to avoid.
    """
    problems: list[str] = []

    if len(questions) != spec.question_count:
        problems.append(
            f"Return exactly {spec.question_count} questions; you returned "
            f"{len(questions)}."
        )

    seen: set[str] = set()
    for question in questions:
        key = _normalised(question.content)
        if key in seen:
            problems.append("Two questions were the same; make every question distinct.")
            break
        seen.add(key)

    types = {question.question_type for question in questions}
    if spec.interview_type == "behavioral" and types - {"behavioral"}:
        problems.append("Ask behavioral questions only.")
    elif spec.interview_type in {"technical", "system_design"} and types - {"technical"}:
        problems.append("Ask technical questions only.")
    elif spec.interview_type == "mixed" and len(questions) > 1 and len(types) < 2:
        problems.append("Mix behavioral and technical questions; you used only one kind.")

    # The point of retrieval is that the interview is about *this* candidate. A
    # set that touches none of their stated skills is the generic interview the
    # fallback would have produced, at the price of a provider call.
    if skills and questions and not _mentions_any(questions, skills):
        problems.append(
            "None of the questions referred to the candidate's own experience; "
            "ground at least one in the resume."
        )

    return Critique(problems)


def _mentions_any(questions: list[GeneratedQuestion], skills: list[str]) -> bool:
    haystack = _normalised(" ".join(question.content for question in questions))
    return any(_normalised(skill) and _normalised(skill) in haystack for skill in skills)


def trim_to_count(questions: list[GeneratedQuestion], count: int) -> list[GeneratedQuestion]:
    """Drop extras. Never pads -- a placeholder question is worse than a short set."""
    return questions[:count] if len(questions) > count else questions
