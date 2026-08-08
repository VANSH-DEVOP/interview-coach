"""The background evaluation worker.

The happy path is covered incidentally by the API tests. What needs asserting
directly is everything that happens when it goes wrong: a provider that raises,
a session deleted mid-flight, and reports abandoned by a restart. Those paths
never run in normal use, so if they are broken nobody finds out until a user is
staring at a spinner that will never resolve.
"""

import uuid

import pytest
from sqlalchemy import select

from app.models.evaluation_report import EvaluationReport, ReportStatus
from app.models.interview_session import InterviewSession, SessionStatus
from app.services import evaluation_worker
from app.services.ai.evaluator import EvaluationResult, Evaluator
from app.services.evaluation_worker import recover_stale_reports, run_evaluation


class _ExplodingEvaluator(Evaluator):
    async def evaluate(self, *, target_role, transcript) -> EvaluationResult:
        raise RuntimeError("provider is down")


@pytest.fixture
async def completed_session(api, registered_user, db_session):
    """A completed session whose report is back in PENDING, ready to evaluate."""
    headers = registered_user["headers"]
    created = await api.post(
        "/api/v1/interviews", json={"title": "Worker", "question_count": 3}, headers=headers
    )
    session_id = uuid.UUID(created.json()["id"])

    detail = await api.get(f"/api/v1/interviews/{session_id}", headers=headers)
    for question in detail.json()["questions"]:
        await api.post(
            f"/api/v1/interviews/{session_id}/answers",
            json={"question_id": question["id"], "content": "A reasonably full answer."},
            headers=headers,
        )
    await api.post(f"/api/v1/interviews/{session_id}/complete", headers=headers)

    report = await _report(db_session, session_id)
    report.status = ReportStatus.PENDING
    report.overall_score = None
    await db_session.commit()

    return session_id, uuid.UUID(registered_user["user"]["id"])


async def _report(db_session, session_id: uuid.UUID) -> EvaluationReport:
    return (
        await db_session.execute(
            select(EvaluationReport).where(EvaluationReport.session_id == session_id)
        )
    ).scalar_one()


async def test_evaluation_populates_the_report(completed_session, db_session):
    session_id, user_id = completed_session

    await run_evaluation(session_id, user_id)

    report = await _report(db_session, session_id)
    await db_session.refresh(report)
    assert report.status is ReportStatus.COMPLETED
    assert report.overall_score is not None
    assert report.strengths


async def test_a_provider_failure_marks_the_report_failed(
    completed_session, db_session, monkeypatch
):
    # Not left on GENERATING: that is indistinguishable from "still working"
    # and the UI would spin forever.
    session_id, user_id = completed_session
    monkeypatch.setattr(
        evaluation_worker, "get_evaluator", lambda: _ExplodingEvaluator()
    )

    await run_evaluation(session_id, user_id)

    report = await _report(db_session, session_id)
    await db_session.refresh(report)
    assert report.status is ReportStatus.FAILED


async def test_run_evaluation_never_raises(completed_session, monkeypatch):
    # It runs as a background task; an escaping exception would vanish into the
    # event loop with nothing recorded anywhere.
    session_id, user_id = completed_session
    monkeypatch.setattr(
        evaluation_worker, "get_evaluator", lambda: _ExplodingEvaluator()
    )

    await run_evaluation(session_id, user_id)  # must not raise


async def test_a_deleted_session_is_not_an_error(api, registered_user, db_session):
    """The user can delete a session between completing it and the worker running."""
    headers = registered_user["headers"]
    created = await api.post(
        "/api/v1/interviews", json={"title": "Gone", "question_count": 3}, headers=headers
    )
    session_id = uuid.UUID(created.json()["id"])
    await api.post(f"/api/v1/interviews/{session_id}/complete", headers=headers)
    await api.delete(f"/api/v1/interviews/{session_id}", headers=headers)

    # No exception, and nothing recreated.
    await run_evaluation(session_id, uuid.UUID(registered_user["user"]["id"]))

    remaining = (
        await db_session.execute(
            select(EvaluationReport).where(EvaluationReport.session_id == session_id)
        )
    ).scalar_one_or_none()
    assert remaining is None


async def test_evaluation_is_scoped_to_the_owner(completed_session, db_session):
    """A worker call with the wrong user id must not evaluate someone's session."""
    session_id, _ = completed_session

    await run_evaluation(session_id, uuid.uuid4())

    report = await _report(db_session, session_id)
    await db_session.refresh(report)
    assert report.status is ReportStatus.PENDING


# -- Restart recovery ----------------------------------------------------------


async def test_recover_stale_reports_fails_reports_left_mid_flight(
    api, registered_user, db_session
):
    headers = registered_user["headers"]
    created = await api.post(
        "/api/v1/interviews", json={"title": "Stranded", "question_count": 3}, headers=headers
    )
    session_id = uuid.UUID(created.json()["id"])
    await api.post(f"/api/v1/interviews/{session_id}/complete", headers=headers)

    # Simulate a process dying mid-evaluation.
    report = await _report(db_session, session_id)
    report.status = ReportStatus.GENERATING
    await db_session.commit()

    recovered = await recover_stale_reports()

    assert recovered >= 1
    await db_session.refresh(report)
    assert report.status is ReportStatus.FAILED


async def test_recover_stale_reports_leaves_finished_reports_alone(
    completed_session, db_session
):
    session_id, user_id = completed_session
    await run_evaluation(session_id, user_id)
    report = await _report(db_session, session_id)
    await db_session.refresh(report)
    assert report.status is ReportStatus.COMPLETED

    await recover_stale_reports()

    await db_session.refresh(report)
    assert report.status is ReportStatus.COMPLETED


async def test_recover_stale_reports_survives_an_unreachable_database(monkeypatch):
    """Startup must not depend on the database being up."""

    def _boom():
        raise ConnectionError("database is not up yet")

    monkeypatch.setattr(evaluation_worker, "AsyncSessionFactory", _boom)

    assert await recover_stale_reports() == 0


# -- Route behaviour -----------------------------------------------------------


async def test_complete_does_not_block_on_evaluation(api, registered_user, db_session):
    """The response must be shaped by the session, not by the provider."""
    headers = registered_user["headers"]
    created = await api.post(
        "/api/v1/interviews", json={"title": "Fast", "question_count": 3}, headers=headers
    )
    session_id = uuid.UUID(created.json()["id"])

    response = await api.post(f"/api/v1/interviews/{session_id}/complete", headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    session = (
        await db_session.execute(
            select(InterviewSession).where(InterviewSession.id == session_id)
        )
    ).scalar_one()
    assert session.status is SessionStatus.COMPLETED


async def test_reevaluate_returns_a_pending_report_before_scoring(
    api, registered_user, monkeypatch
):
    headers = registered_user["headers"]
    created = await api.post(
        "/api/v1/interviews", json={"title": "Retry", "question_count": 3}, headers=headers
    )
    session_id = created.json()["id"]
    await api.post(f"/api/v1/interviews/{session_id}/complete", headers=headers)

    # Stop the queued work from running so the immediate response is visible.
    monkeypatch.setattr(evaluation_worker, "get_evaluator", lambda: _ExplodingEvaluator())

    response = await api.post(f"/api/v1/interviews/{session_id}/reevaluate", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["overall_score"] is None
