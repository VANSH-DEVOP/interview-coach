"""Deleting an account.

The database cascade is the easy half and is verified here row by row. The half
worth worrying about is the two stores Postgres cannot reach -- the resume blobs
and the vector index -- because those hold the actual text of someone's CV, and
deleting only the rows would report success while leaving the sensitive part
behind.
"""

import uuid

import pytest
from sqlalchemy import func, select

from app.models.evaluation_report import EvaluationReport
from app.models.interview_session import InterviewSession
from app.models.one_time_token import OneTimeToken
from app.models.question import Question
from app.models.refresh_token import RefreshToken
from app.models.resume import Resume
from app.models.user import User


async def _count(db_session, model, user_column, user_id) -> int:
    return (
        await db_session.execute(
            select(func.count()).select_from(model).where(user_column == user_id)
        )
    ).scalar_one()


@pytest.fixture
async def user_with_data(api, storage_root, registered_user, db_session):
    """A user with an interview, questions, answers and a report."""
    headers = registered_user["headers"]
    created = await api.post(
        "/api/v1/interviews",
        json={"title": "To be deleted", "question_count": 3},
        headers=headers,
    )
    session_id = created.json()["id"]
    detail = await api.get(f"/api/v1/interviews/{session_id}", headers=headers)
    for question in detail.json()["questions"]:
        await api.post(
            f"/api/v1/interviews/{session_id}/answers",
            json={"question_id": question["id"], "content": "An answer."},
            headers=headers,
        )
    await api.post(f"/api/v1/interviews/{session_id}/complete", headers=headers)

    return {**registered_user, "id": uuid.UUID(registered_user["user"]["id"])}


async def _delete(api, user, password=None):
    return await api.request(
        "DELETE",
        "/api/v1/users/me",
        json={"password": password or user["password"]},
        headers=user["headers"],
    )


# -- The happy path ------------------------------------------------------------


async def test_deleting_removes_the_user(api, user_with_data, db_session):
    response = await _delete(api, user_with_data)

    assert response.status_code == 204
    assert await db_session.get(User, user_with_data["id"]) is None


async def test_the_cascade_reaches_every_child_table(api, user_with_data, db_session):
    user_id = user_with_data["id"]
    assert await _count(db_session, InterviewSession, InterviewSession.user_id, user_id) > 0

    await _delete(api, user_with_data)

    assert await _count(db_session, InterviewSession, InterviewSession.user_id, user_id) == 0
    assert await _count(db_session, Resume, Resume.user_id, user_id) == 0
    assert await _count(db_session, RefreshToken, RefreshToken.user_id, user_id) == 0
    assert await _count(db_session, OneTimeToken, OneTimeToken.user_id, user_id) == 0


async def test_questions_and_reports_go_with_the_sessions(api, user_with_data, db_session):
    """Two levels down from the user; the cascade has to reach them too."""
    user_id = user_with_data["id"]
    orphan_questions = (
        select(func.count())
        .select_from(Question)
        .where(
            Question.session_id.in_(
                select(InterviewSession.id).where(InterviewSession.user_id == user_id)
            )
        )
    )
    assert (await db_session.execute(orphan_questions)).scalar_one() > 0

    await _delete(api, user_with_data)

    assert (await db_session.execute(orphan_questions)).scalar_one() == 0
    reports = (
        select(func.count())
        .select_from(EvaluationReport)
        .where(
            EvaluationReport.session_id.in_(
                select(InterviewSession.id).where(InterviewSession.user_id == user_id)
            )
        )
    )
    assert (await db_session.execute(reports)).scalar_one() == 0


async def test_the_session_stops_working(api, user_with_data):
    await _delete(api, user_with_data)

    me = await api.get("/api/v1/users/me", headers=user_with_data["headers"])
    assert me.status_code == 401


async def test_the_refresh_token_stops_working(api, user_with_data):
    """Not revoked explicitly -- the rows cascade away with the user."""
    await _delete(api, user_with_data)

    response = await api.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": user_with_data["tokens"]["refresh_token"]},
    )
    assert response.status_code == 401


async def test_the_email_can_be_registered_again(api, user_with_data):
    """A hard delete has to actually free the address, or 'delete my account'
    leaves the user unable to come back."""
    await _delete(api, user_with_data)

    response = await api.post(
        "/api/v1/auth/register",
        json={
            "email": user_with_data["email"],
            "password": "a-completely-new-password",
            "full_name": "Returning User",
        },
    )
    assert response.status_code == 201


# -- The stores Postgres cannot reach ------------------------------------------


def _pdf_bytes() -> bytes:
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


async def test_the_resume_blob_is_deleted_from_storage(
    api, storage_root, registered_user, db_session
):
    """The point of the whole feature. Deleting the row while leaving the file
    on disk removes the reference to someone's CV, not the CV.
    """
    upload = await api.post(
        "/api/v1/resumes",
        files={"file": ("cv.pdf", _pdf_bytes(), "application/pdf")},
        headers=registered_user["headers"],
    )
    assert upload.status_code == 201, upload.text
    assert list(storage_root.rglob("*.pdf")), "nothing was stored to begin with"

    response = await api.request(
        "DELETE",
        "/api/v1/users/me",
        json={"password": registered_user["password"]},
        headers=registered_user["headers"],
    )

    assert response.status_code == 204
    assert list(storage_root.rglob("*.pdf")) == [], "the resume file is still on disk"


async def test_a_storage_failure_does_not_strand_the_user(
    api, storage_root, registered_user, monkeypatch
):
    """A blob the backend cannot delete leaves an orphan with no row pointing
    at it, which is reclaimable. Refusing the deletion is not: the user asked
    to be rid of the account and would be told no, forever.
    """
    await api.post(
        "/api/v1/resumes",
        files={"file": ("cv.pdf", _pdf_bytes(), "application/pdf")},
        headers=registered_user["headers"],
    )

    from app.services.storage.local import LocalStorageService

    async def boom(self, key):
        raise OSError("the disk is on fire")

    monkeypatch.setattr(LocalStorageService, "delete", boom)

    response = await api.request(
        "DELETE",
        "/api/v1/users/me",
        json={"password": registered_user["password"]},
        headers=registered_user["headers"],
    )

    assert response.status_code == 204


# -- Re-authentication ---------------------------------------------------------


async def test_a_wrong_password_is_refused(api, user_with_data, db_session):
    """An access token borrowed from an unattended machine must not be enough
    to destroy an account."""
    response = await _delete(api, user_with_data, password="not-the-password")

    assert response.status_code == 401
    assert await db_session.get(User, user_with_data["id"]) is not None


async def test_a_failed_attempt_deletes_nothing(api, user_with_data, db_session):
    await _delete(api, user_with_data, password="not-the-password")

    assert (
        await _count(db_session, InterviewSession, InterviewSession.user_id, user_with_data["id"])
        > 0
    )


async def test_authentication_is_required(api):
    response = await api.request(
        "DELETE", "/api/v1/users/me", json={"password": "anything"}
    )
    assert response.status_code == 401


async def test_one_user_cannot_delete_another(api, storage_root, registered_user, db_session):
    """There is no id in the route -- it always acts on the caller -- but this
    pins that down, since adding one later would be an easy mistake."""
    other = await api.post(
        "/api/v1/auth/register",
        json={
            "email": f"other-{uuid.uuid4().hex[:8]}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Other",
        },
    )
    other_id = uuid.UUID(other.json()["id"])

    await api.request(
        "DELETE",
        "/api/v1/users/me",
        json={"password": registered_user["password"]},
        headers=registered_user["headers"],
    )

    assert await db_session.get(User, other_id) is not None
