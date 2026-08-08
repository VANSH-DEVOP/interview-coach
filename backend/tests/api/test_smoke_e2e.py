"""End-to-end smoke test: the whole product in one pass.

Every other test checks a slice. This one walks the journey a real user takes
-- register, upload a resume, run an interview against it, finish, read the
report, see it in progress tracking -- because the failures that hurt most are
the ones between the slices. The follow-up branch that raised MissingGreenlet
under asyncio passed every unit test in this repository; only running the flow
end to end would have caught it.

Deliberately asserts on the *contract* (status codes, shapes, linkage) rather
than on AI wording, so it stays meaningful with the deterministic fallbacks
that the `api` fixture forces on.
"""

import io
import uuid

import pytest
from pypdf import PdfWriter

PDF_TYPE = "application/pdf"


def _resume_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pytest.fixture
def storage_root(tmp_path, monkeypatch):
    from app.core.config import get_settings
    from app.services.storage import get_storage_service

    monkeypatch.setattr(get_settings(), "STORAGE_LOCAL_PATH", tmp_path)
    get_storage_service.cache_clear()
    yield tmp_path
    get_storage_service.cache_clear()


async def test_full_journey_register_to_report(api, storage_root):
    email = f"smoke-{uuid.uuid4().hex[:10]}@example.com"
    password = "correct-horse-battery"

    # 1. Register and log in.
    register = await api.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Smoke Test"},
    )
    assert register.status_code == 201, register.text

    login = await api.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    # 2. A brand-new account starts empty everywhere.
    progress = (await api.get("/api/v1/reports/progress", headers=headers)).json()
    assert progress["total_scored"] == 0
    assert progress["average_score"] is None

    # 3. Upload a resume.
    upload = await api.post(
        "/api/v1/resumes",
        files={"file": ("cv.pdf", _resume_pdf(), PDF_TYPE)},
        headers=headers,
    )
    assert upload.status_code == 201, upload.text
    resume_id = upload.json()["id"]

    # 4. Start an interview against that resume.
    create = await api.post(
        "/api/v1/interviews",
        json={
            "title": "Backend practice",
            "target_role": "Backend Engineer",
            "resume_id": resume_id,
            "interview_type": "technical",
            "difficulty": "mid",
            "question_count": 3,
        },
        headers=headers,
    )
    assert create.status_code == 201, create.text
    session = create.json()
    session_id = session["id"]
    assert session["status"] == "in_progress"
    assert session["resume_id"] == resume_id

    # 5. Answer every question.
    detail = await api.get(f"/api/v1/interviews/{session_id}", headers=headers)
    questions = detail.json()["questions"]
    assert len(questions) == 3

    for index, question in enumerate(questions):
        answer = await api.post(
            f"/api/v1/interviews/{session_id}/answers",
            json={
                "question_id": question["id"],
                "content": (
                    "I led the migration to async SQLAlchemy and cut p99 latency "
                    "by roughly forty percent, measured before and after."
                ),
                "duration_seconds": 30 + index,
            },
            headers=headers,
        )
        assert answer.status_code == 201, answer.text

    # 6. Every answer is attached, with its timing.
    answered = (await api.get(f"/api/v1/interviews/{session_id}", headers=headers)).json()
    originals = [q for q in answered["questions"] if q["id"] in {q2["id"] for q2 in questions}]
    assert all(q["answer"] is not None for q in originals)
    assert [q["answer"]["duration_seconds"] for q in originals] == [30, 31, 32]

    # 7. Finish the interview.
    complete = await api.post(f"/api/v1/interviews/{session_id}/complete", headers=headers)
    assert complete.status_code == 200, complete.text
    assert complete.json()["status"] == "completed"
    assert complete.json()["completed_at"] is not None

    # 8. The report exists, is populated, and is reachable both ways.
    by_session = await api.get(f"/api/v1/reports/by-session/{session_id}", headers=headers)
    assert by_session.status_code == 200, by_session.text
    report = by_session.json()

    assert report["session_id"] == session_id
    assert report["status"] == "completed"
    assert report["overall_score"] is not None
    assert float(report["overall_score"]) > 0
    assert report["strengths"], "a completed report must always be populated"

    feedback = report["detailed_feedback"]
    assert feedback["summary"], "the UI renders this as the headline"
    per_question = feedback["per_question"]
    assert len(per_question) == len(answered["questions"])
    assert all(entry["score"] is not None for entry in per_question)

    by_id = await api.get(f"/api/v1/reports/{report['id']}", headers=headers)
    assert by_id.status_code == 200
    assert by_id.json()["id"] == report["id"]

    # 9. It shows up in the listings.
    listed = await api.get("/api/v1/reports?page=1&size=20", headers=headers)
    assert listed.json()["total"] == 1

    # 10. And in progress tracking.
    progress = (await api.get("/api/v1/reports/progress", headers=headers)).json()
    assert progress["total_scored"] == 1
    assert progress["latest_score"] == pytest.approx(float(report["overall_score"]))
    assert progress["points"][0]["session_id"] == session_id
    # One interview is not a trend.
    assert progress["improvement"] is None

    # 11. Re-evaluating replaces the report rather than duplicating it.
    reevaluated = await api.post(f"/api/v1/interviews/{session_id}/reevaluate", headers=headers)
    assert reevaluated.status_code == 200
    assert reevaluated.json()["id"] == report["id"]
    assert (await api.get("/api/v1/reports?page=1&size=20", headers=headers)).json()["total"] == 1

    # 12. Deleting the session takes the report with it and leaves the
    #     account clean -- no orphans in any listing.
    assert (
        await api.delete(f"/api/v1/interviews/{session_id}", headers=headers)
    ).status_code == 204
    assert (await api.get("/api/v1/reports?page=1&size=20", headers=headers)).json()["total"] == 0
    assert (
        await api.get("/api/v1/interviews?page=1&size=20", headers=headers)
    ).json()["total"] == 0
    assert (
        await api.get("/api/v1/reports/progress", headers=headers)
    ).json()["total_scored"] == 0
    # The resume is independent of the session and survives.
    assert (await api.get(f"/api/v1/resumes/{resume_id}", headers=headers)).status_code == 200


async def test_abandoned_journey_leaves_no_report(api, storage_root):
    """The other realistic path: a user gives up partway through."""
    email = f"quit-{uuid.uuid4().hex[:10]}@example.com"
    password = "correct-horse-battery"
    await api.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Quitter"},
    )
    login = await api.post("/api/v1/auth/login", json={"email": email, "password": password})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    session = (
        await api.post(
            "/api/v1/interviews",
            json={"title": "Half-finished", "question_count": 3},
            headers=headers,
        )
    ).json()
    questions = (
        await api.get(f"/api/v1/interviews/{session['id']}", headers=headers)
    ).json()["questions"]

    await api.post(
        f"/api/v1/interviews/{session['id']}/answers",
        json={"question_id": questions[0]["id"], "content": "Only answering one."},
        headers=headers,
    )

    abandoned = await api.post(f"/api/v1/interviews/{session['id']}/abandon", headers=headers)
    assert abandoned.status_code == 200
    assert abandoned.json()["status"] == "abandoned"

    # No report, and nothing polluting the score trend.
    assert (
        await api.get(f"/api/v1/reports/by-session/{session['id']}", headers=headers)
    ).status_code == 404
    assert (
        await api.get("/api/v1/reports/progress", headers=headers)
    ).json()["total_scored"] == 0

    # But the transcript is still there to look back at.
    kept = (await api.get(f"/api/v1/interviews/{session['id']}", headers=headers)).json()
    assert len(kept["questions"]) == 3
    assert kept["questions"][0]["answer"]["content"] == "Only answering one."
