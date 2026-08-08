"""Markdown export of a report.

The renderer's job is to survive whatever the model left in JSONB and still
produce something a person can read and share.
"""

import uuid

import pytest
from sqlalchemy import select

from app.models.evaluation_report import EvaluationReport
from app.models.interview_session import InterviewSession
from app.services.report_export import filename_for, render_markdown


@pytest.fixture
async def other_user(api):
    email = f"other-{uuid.uuid4().hex[:8]}@example.com"
    password = "correct-horse-battery"
    await api.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Other"},
    )
    login = await api.post("/api/v1/auth/login", json={"email": email, "password": password})
    return {"headers": {"Authorization": f"Bearer {login.json()['access_token']}"}}


@pytest.fixture
async def report(api, registered_user, db_session):
    """A completed session and its report id."""
    headers = registered_user["headers"]
    created = await api.post(
        "/api/v1/interviews",
        json={"title": "Backend practice", "target_role": "Backend Engineer", "question_count": 3},
        headers=headers,
    )
    session_id = uuid.UUID(created.json()["id"])

    detail = await api.get(f"/api/v1/interviews/{session_id}", headers=headers)
    for question in detail.json()["questions"]:
        await api.post(
            f"/api/v1/interviews/{session_id}/answers",
            json={"question_id": question["id"], "content": "A reasonably detailed answer."},
            headers=headers,
        )
    await api.post(f"/api/v1/interviews/{session_id}/complete", headers=headers)

    row = (
        await db_session.execute(
            select(EvaluationReport).where(EvaluationReport.session_id == session_id)
        )
    ).scalar_one()
    return {"id": row.id, "session_id": session_id, "headers": headers}


# -- HTTP ----------------------------------------------------------------------


async def test_export_downloads_markdown(api, report):
    response = await api.get(f"/api/v1/reports/{report['id']}/export", headers=report["headers"])

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "attachment" in response.headers["content-disposition"]
    assert ".md" in response.headers["content-disposition"]


async def test_export_contains_the_report_content(api, report):
    response = await api.get(f"/api/v1/reports/{report['id']}/export", headers=report["headers"])
    body = response.text

    assert "# Interview report — Backend practice" in body
    assert "Backend Engineer" in body
    assert "## Overall score" in body
    assert "## Strengths" in body
    assert "## Areas to improve" in body


async def test_export_requires_authentication(api, report):
    assert (await api.get(f"/api/v1/reports/{report['id']}/export")).status_code == 401


async def test_another_user_cannot_export(api, report, other_user):
    response = await api.get(
        f"/api/v1/reports/{report['id']}/export", headers=other_user["headers"]
    )
    assert response.status_code == 404


async def test_export_unknown_report_is_404(api, registered_user):
    response = await api.get(
        f"/api/v1/reports/{uuid.uuid4()}/export", headers=registered_user["headers"]
    )
    assert response.status_code == 404


# -- Renderer ------------------------------------------------------------------


def _session(title="My interview") -> InterviewSession:
    from app.models.interview_session import DifficultyLevel, InterviewType

    session = InterviewSession(
        user_id=uuid.uuid4(),
        title=title,
        target_role="Backend Engineer",
        interview_type=InterviewType.SYSTEM_DESIGN,
        difficulty=DifficultyLevel.SENIOR,
        question_count=3,
    )
    session.completed_at = None
    return session


def test_render_handles_a_report_with_nothing_populated():
    """A failed evaluation still has to export without blowing up."""
    report = EvaluationReport(
        session_id=uuid.uuid4(),
        overall_score=None,
        strengths=None,
        weaknesses=None,
        detailed_feedback=None,
    )

    markdown = render_markdown(report, _session())

    assert "not scored" in markdown
    assert "_None identified._" in markdown


def test_render_survives_non_string_items_in_jsonb():
    # The columns hold whatever the model produced; dicts have shown up before.
    report = EvaluationReport(
        session_id=uuid.uuid4(),
        overall_score=None,
        strengths=[{"point": "structured"}, "Clear communication", "", 42],
        weaknesses=["Needs metrics"],
        detailed_feedback={"per_question": ["not a dict", {"question": "Q1", "score": 8}]},
    )

    markdown = render_markdown(report, _session())

    assert "- Clear communication" in markdown
    # The dict and the blank are dropped rather than rendered as junk.
    assert "point" not in markdown
    assert "Q1 — 8/10" in markdown


def test_render_includes_per_question_scores():
    report = EvaluationReport(
        session_id=uuid.uuid4(),
        overall_score=None,
        strengths=[],
        weaknesses=[],
        detailed_feedback={
            "summary": "A solid showing.",
            "recommendations": ["Use the STAR method."],
            "per_question": [
                {"question": "Tell me about yourself.", "score": 7.5, "feedback": "Good."}
            ],
        },
    )

    markdown = render_markdown(report, _session())

    assert "A solid showing." in markdown
    assert "## Recommendations" in markdown
    assert "### 1. Tell me about yourself. — 7.5/10" in markdown
    assert "Good." in markdown


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Backend practice", "interview-report-Backend-practice.md"),
        ("../../etc/passwd", "interview-report-etcpasswd.md"),
        ('quote"; drop', "interview-report-quote-drop.md"),
        ("   ", "interview-report-report.md"),
    ],
)
def test_filename_is_safe(title, expected):
    """The title is user-supplied and lands in a Content-Disposition header."""
    assert filename_for(_session(title)) == expected


def test_filename_is_length_capped():
    name = filename_for(_session("word " * 100))
    assert len(name) <= len("interview-report-") + 60 + len(".md")
