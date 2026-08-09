"""The handoff to the evaluation runner must happen after a commit.

This is the regression test for a bug that shipped and survived a full suite of
green tests: `complete()` only flushed, so the PENDING report and the completed
session existed nowhere outside the request's own transaction. The runner opens
its own session, found neither, logged "session no longer exists", and returned
-- leaving the report on PENDING forever with no error anywhere.

Every existing test missed it because the `api` fixture points the runner's
session factory at the test's own connection, so a flush is visible to it. That
fixture is what makes the other tests possible; it also makes this class of bug
invisible, which is why the check here is on the commit itself rather than on
the report status.
"""

import uuid

import pytest


@pytest.fixture
def commits(db_session, monkeypatch):
    """Counts commits on the request's session."""
    counter = {"n": 0}
    original = db_session.commit

    async def counting_commit(*args, **kwargs):
        counter["n"] += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(db_session, "commit", counting_commit)
    return counter


@pytest.fixture
async def answered_session(api, registered_user):
    """An in-progress interview with every question answered."""
    headers = registered_user["headers"]
    created = await api.post(
        "/api/v1/interviews",
        json={"title": "Handoff", "question_count": 3},
        headers=headers,
    )
    session_id = created.json()["id"]
    detail = await api.get(f"/api/v1/interviews/{session_id}", headers=headers)
    for question in detail.json()["questions"]:
        await api.post(
            f"/api/v1/interviews/{session_id}/answers",
            json={"question_id": question["id"], "content": "A detailed answer."},
            headers=headers,
        )
    return {"id": session_id, "headers": headers}


async def test_completing_commits_before_handing_off(api, answered_session, commits):
    """A flush here is invisible to the runner's session. Only a commit is not."""
    response = await api.post(
        f"/api/v1/interviews/{answered_session['id']}/complete",
        headers=answered_session["headers"],
    )

    assert response.status_code == 200
    assert commits["n"] >= 1, (
        "complete() did not commit. The evaluation runner opens its own session "
        "and will not see the session or the PENDING report."
    )


async def test_reevaluating_commits_before_handing_off(api, answered_session, commits):
    headers = answered_session["headers"]
    await api.post(f"/api/v1/interviews/{answered_session['id']}/complete", headers=headers)
    before = commits["n"]

    response = await api.post(
        f"/api/v1/interviews/{answered_session['id']}/reevaluate", headers=headers
    )

    assert response.status_code == 200
    assert commits["n"] > before, "reevaluate() did not commit the reset report."


async def test_the_response_is_still_usable_after_the_commit(api, answered_session):
    """expire_on_commit=False is load-bearing here: the route serialises the
    session object *after* complete() commits, and an expired instance would
    need a refresh the route does not do."""
    response = await api.post(
        f"/api/v1/interviews/{answered_session['id']}/complete",
        headers=answered_session["headers"],
    )

    body = response.json()
    assert body["status"] == "completed"
    assert body["completed_at"] is not None
    assert uuid.UUID(body["id"]) == uuid.UUID(answered_session["id"])
