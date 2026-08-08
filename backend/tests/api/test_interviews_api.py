"""Interview and report endpoints over HTTP, against a real database.

The ownership tests matter most here: the in-memory fakes used by the service
tests implement `get_owned` themselves, so they prove the service *calls* it,
not that the SQL actually filters by user. Only a real database shows that.
"""

import uuid

import pytest


@pytest.fixture
async def other_user(api):
    """A second registered user, for isolation checks."""
    email = f"other-{uuid.uuid4().hex[:8]}@example.com"
    password = "correct-horse-battery"
    await api.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Other"},
    )
    login = await api.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    return {"headers": {"Authorization": f"Bearer {login.json()['access_token']}"}}


async def _create_session(api, headers, **overrides):
    payload = {"title": "Practice", "question_count": 3, **overrides}
    response = await api.post("/api/v1/interviews", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def _questions(api, headers, session_id):
    response = await api.get(f"/api/v1/interviews/{session_id}", headers=headers)
    return response.json()["questions"]


# -- Creation ------------------------------------------------------------------


async def test_create_generates_and_persists_questions(api, registered_user):
    session = await _create_session(api, registered_user["headers"], question_count=4)

    assert session["status"] == "in_progress"
    questions = await _questions(api, registered_user["headers"], session["id"])
    assert len(questions) == 4
    assert all(q["answer"] is None for q in questions)
    assert [q["sequence_number"] for q in questions] == [1, 2, 3, 4]


async def test_create_persists_the_configuration(api, registered_user):
    session = await _create_session(
        api,
        registered_user["headers"],
        interview_type="system_design",
        difficulty="senior",
        question_count=6,
    )

    assert session["interview_type"] == "system_design"
    assert session["difficulty"] == "senior"
    assert session["question_count"] == 6


@pytest.mark.parametrize("count", [2, 11])
async def test_create_rejects_an_out_of_range_question_count(api, registered_user, count):
    response = await api.post(
        "/api/v1/interviews",
        json={"title": "Practice", "question_count": count},
        headers=registered_user["headers"],
    )
    assert response.status_code == 422


async def test_create_rejects_an_unknown_resume(api, registered_user):
    response = await api.post(
        "/api/v1/interviews",
        json={"title": "Practice", "resume_id": str(uuid.uuid4())},
        headers=registered_user["headers"],
    )
    assert response.status_code == 404


async def test_create_requires_authentication(api):
    assert (await api.post("/api/v1/interviews", json={"title": "x"})).status_code == 401


# -- Answering -----------------------------------------------------------------


async def test_submit_answer_attaches_it_to_the_question(api, registered_user):
    headers = registered_user["headers"]
    session = await _create_session(api, headers)
    question = (await _questions(api, headers, session["id"]))[0]

    response = await api.post(
        f"/api/v1/interviews/{session['id']}/answers",
        json={"question_id": question["id"], "content": "My answer.", "duration_seconds": 42},
        headers=headers,
    )

    assert response.status_code == 201
    assert response.json()["duration_seconds"] == 42

    refreshed = (await _questions(api, headers, session["id"]))[0]
    assert refreshed["answer"]["content"] == "My answer."


async def test_answering_the_same_question_twice_conflicts(api, registered_user):
    headers = registered_user["headers"]
    session = await _create_session(api, headers)
    question = (await _questions(api, headers, session["id"]))[0]
    body = {"question_id": question["id"], "content": "First."}

    assert (
        await api.post(f"/api/v1/interviews/{session['id']}/answers", json=body, headers=headers)
    ).status_code == 201
    second = await api.post(
        f"/api/v1/interviews/{session['id']}/answers", json=body, headers=headers
    )

    assert second.status_code == 409


async def test_answer_for_a_question_in_another_session_is_rejected(api, registered_user):
    headers = registered_user["headers"]
    first = await _create_session(api, headers)
    second = await _create_session(api, headers)
    foreign_question = (await _questions(api, headers, second["id"]))[0]

    response = await api.post(
        f"/api/v1/interviews/{first['id']}/answers",
        json={"question_id": foreign_question["id"], "content": "Wrong session."},
        headers=headers,
    )

    assert response.status_code == 404


async def test_empty_answer_is_rejected(api, registered_user):
    headers = registered_user["headers"]
    session = await _create_session(api, headers)
    question = (await _questions(api, headers, session["id"]))[0]

    response = await api.post(
        f"/api/v1/interviews/{session['id']}/answers",
        json={"question_id": question["id"], "content": ""},
        headers=headers,
    )

    assert response.status_code == 422


# -- Lifecycle -----------------------------------------------------------------


async def test_complete_produces_a_report(api, registered_user):
    headers = registered_user["headers"]
    session = await _create_session(api, headers)
    for question in await _questions(api, headers, session["id"]):
        await api.post(
            f"/api/v1/interviews/{session['id']}/answers",
            json={"question_id": question["id"], "content": "A reasonably detailed answer."},
            headers=headers,
        )

    completed = await api.post(f"/api/v1/interviews/{session['id']}/complete", headers=headers)
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"

    report = await api.get(f"/api/v1/reports/by-session/{session['id']}", headers=headers)
    assert report.status_code == 200
    body = report.json()
    assert body["status"] == "completed"
    assert body["overall_score"] is not None
    assert body["detailed_feedback"]["summary"]


async def test_completing_twice_conflicts(api, registered_user):
    headers = registered_user["headers"]
    session = await _create_session(api, headers)
    await api.post(f"/api/v1/interviews/{session['id']}/complete", headers=headers)

    again = await api.post(f"/api/v1/interviews/{session['id']}/complete", headers=headers)
    assert again.status_code == 409


async def test_abandon_keeps_the_transcript_and_blocks_answers(api, registered_user):
    headers = registered_user["headers"]
    session = await _create_session(api, headers)
    question = (await _questions(api, headers, session["id"]))[0]

    abandoned = await api.post(f"/api/v1/interviews/{session['id']}/abandon", headers=headers)
    assert abandoned.status_code == 200
    assert abandoned.json()["status"] == "abandoned"

    assert len(await _questions(api, headers, session["id"])) == 3

    late = await api.post(
        f"/api/v1/interviews/{session['id']}/answers",
        json={"question_id": question["id"], "content": "Too late."},
        headers=headers,
    )
    assert late.status_code == 409


async def test_delete_cascades_to_the_report(api, registered_user):
    headers = registered_user["headers"]
    session = await _create_session(api, headers)
    await api.post(f"/api/v1/interviews/{session['id']}/complete", headers=headers)
    assert (
        await api.get(f"/api/v1/reports/by-session/{session['id']}", headers=headers)
    ).status_code == 200

    assert (
        await api.delete(f"/api/v1/interviews/{session['id']}", headers=headers)
    ).status_code == 204

    assert (
        await api.get(f"/api/v1/interviews/{session['id']}", headers=headers)
    ).status_code == 404
    assert (
        await api.get(f"/api/v1/reports/by-session/{session['id']}", headers=headers)
    ).status_code == 404


async def test_reevaluate_updates_the_report_in_place(api, registered_user):
    headers = registered_user["headers"]
    session = await _create_session(api, headers)
    await api.post(f"/api/v1/interviews/{session['id']}/complete", headers=headers)
    before = (
        await api.get(f"/api/v1/reports/by-session/{session['id']}", headers=headers)
    ).json()

    response = await api.post(f"/api/v1/interviews/{session['id']}/reevaluate", headers=headers)

    assert response.status_code == 200
    assert response.json()["id"] == before["id"], "a new report would orphan the old one"


async def test_reevaluate_requires_a_completed_session(api, registered_user):
    session = await _create_session(api, registered_user["headers"])
    response = await api.post(
        f"/api/v1/interviews/{session['id']}/reevaluate", headers=registered_user["headers"]
    )
    assert response.status_code == 409


# -- Ownership -----------------------------------------------------------------


async def test_another_user_cannot_read_a_session(api, registered_user, other_user):
    session = await _create_session(api, registered_user["headers"])

    response = await api.get(f"/api/v1/interviews/{session['id']}", headers=other_user["headers"])

    # 404, not 403: a stranger learns nothing about whether the id exists.
    assert response.status_code == 404


@pytest.mark.parametrize("action", ["abandon", "complete", "reevaluate"])
async def test_another_user_cannot_act_on_a_session(api, registered_user, other_user, action):
    session = await _create_session(api, registered_user["headers"])

    response = await api.post(
        f"/api/v1/interviews/{session['id']}/{action}", headers=other_user["headers"]
    )

    assert response.status_code == 404


async def test_another_user_cannot_delete_a_session(api, registered_user, other_user):
    session = await _create_session(api, registered_user["headers"])

    assert (
        await api.delete(f"/api/v1/interviews/{session['id']}", headers=other_user["headers"])
    ).status_code == 404
    # Still there for its owner.
    assert (
        await api.get(f"/api/v1/interviews/{session['id']}", headers=registered_user["headers"])
    ).status_code == 200


async def test_listing_is_scoped_to_the_authenticated_user(api, registered_user, other_user):
    await _create_session(api, registered_user["headers"], title="Mine")

    listed = await api.get("/api/v1/interviews?page=1&size=50", headers=other_user["headers"])

    assert listed.status_code == 200
    assert listed.json()["total"] == 0


async def test_another_user_cannot_read_a_report(api, registered_user, other_user):
    session = await _create_session(api, registered_user["headers"])
    await api.post(f"/api/v1/interviews/{session['id']}/complete", headers=registered_user["headers"])
    report = (
        await api.get(
            f"/api/v1/reports/by-session/{session['id']}", headers=registered_user["headers"]
        )
    ).json()

    by_session = await api.get(
        f"/api/v1/reports/by-session/{session['id']}", headers=other_user["headers"]
    )
    by_id = await api.get(f"/api/v1/reports/{report['id']}", headers=other_user["headers"])

    assert by_session.status_code == 404
    assert by_id.status_code == 404


# -- Pagination and progress ---------------------------------------------------


async def test_pagination_does_not_overlap_or_drop_records(api, registered_user):
    headers = registered_user["headers"]
    for i in range(5):
        await _create_session(api, headers, title=f"Session {i}")

    seen: list[str] = []
    for page in (1, 2, 3):
        body = (await api.get(f"/api/v1/interviews?page={page}&size=2", headers=headers)).json()
        assert body["total"] == 5
        seen += [item["id"] for item in body["items"]]

    assert len(seen) == len(set(seen)) == 5


async def test_progress_is_empty_before_any_scored_session(api, registered_user):
    body = (await api.get("/api/v1/reports/progress", headers=registered_user["headers"])).json()

    assert body["total_scored"] == 0
    # Null rather than 0: "no interviews yet" is not "you scored zero".
    assert body["average_score"] is None
    assert body["improvement"] is None


async def test_progress_reflects_completed_sessions(api, registered_user):
    headers = registered_user["headers"]
    for i in range(2):
        session = await _create_session(api, headers, title=f"S{i}")
        await api.post(f"/api/v1/interviews/{session['id']}/complete", headers=headers)

    body = (await api.get("/api/v1/reports/progress", headers=headers)).json()

    assert body["total_scored"] == 2
    assert body["average_score"] is not None
    timestamps = [p["scored_at"] for p in body["points"]]
    assert timestamps == sorted(timestamps), "points must be chronological"


async def test_progress_is_scoped_to_the_user(api, registered_user, other_user):
    session = await _create_session(api, registered_user["headers"])
    await api.post(f"/api/v1/interviews/{session['id']}/complete", headers=registered_user["headers"])

    body = (await api.get("/api/v1/reports/progress", headers=other_user["headers"])).json()

    assert body["total_scored"] == 0


async def test_progress_route_is_not_shadowed_by_the_report_id_route(api, registered_user):
    """/reports/progress must not be parsed as /reports/{report_id}."""
    response = await api.get("/api/v1/reports/progress", headers=registered_user["headers"])
    assert response.status_code == 200
