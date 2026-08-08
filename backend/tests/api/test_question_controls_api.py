"""Skip, re-answer, and regenerate.

These are the escape hatches for a stuck interview: a question the candidate
cannot answer, an answer they want to redo, and a question set that missed the
mark. Each has a state rule that only shows up when it is violated.
"""

import uuid

import pytest


async def _session(api, headers, **overrides):
    payload = {"title": "Controls", "question_count": 3, **overrides}
    response = await api.post("/api/v1/interviews", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def _questions(api, headers, session_id):
    detail = await api.get(f"/api/v1/interviews/{session_id}", headers=headers)
    return detail.json()["questions"]


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


# -- Skip ----------------------------------------------------------------------


async def test_skip_marks_the_question(api, registered_user):
    headers = registered_user["headers"]
    session = await _session(api, headers)
    question = (await _questions(api, headers, session["id"]))[0]

    response = await api.post(
        f"/api/v1/interviews/{session['id']}/questions/{question['id']}/skip",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["skipped"] is True

    # Persisted, and still visible in the transcript.
    refreshed = (await _questions(api, headers, session["id"]))[0]
    assert refreshed["skipped"] is True
    assert refreshed["answer"] is None


async def test_a_skipped_question_can_still_be_answered(api, registered_user):
    """Skipping is a decision, not a lock -- changing your mind must work."""
    headers = registered_user["headers"]
    session = await _session(api, headers)
    question = (await _questions(api, headers, session["id"]))[0]
    await api.post(
        f"/api/v1/interviews/{session['id']}/questions/{question['id']}/skip", headers=headers
    )

    answered = await api.post(
        f"/api/v1/interviews/{session['id']}/answers",
        json={"question_id": question["id"], "content": "Actually, here is my answer."},
        headers=headers,
    )

    assert answered.status_code == 201
    refreshed = (await _questions(api, headers, session["id"]))[0]
    # The skip is withdrawn; leaving it set would contradict the answer.
    assert refreshed["skipped"] is False
    assert refreshed["answer"] is not None


async def test_an_answered_question_cannot_be_skipped(api, registered_user):
    headers = registered_user["headers"]
    session = await _session(api, headers)
    question = (await _questions(api, headers, session["id"]))[0]
    await api.post(
        f"/api/v1/interviews/{session['id']}/answers",
        json={"question_id": question["id"], "content": "Already answered."},
        headers=headers,
    )

    response = await api.post(
        f"/api/v1/interviews/{session['id']}/questions/{question['id']}/skip", headers=headers
    )

    assert response.status_code == 409


async def test_skip_requires_an_in_progress_session(api, registered_user):
    headers = registered_user["headers"]
    session = await _session(api, headers)
    question = (await _questions(api, headers, session["id"]))[0]
    await api.post(f"/api/v1/interviews/{session['id']}/complete", headers=headers)

    response = await api.post(
        f"/api/v1/interviews/{session['id']}/questions/{question['id']}/skip", headers=headers
    )

    assert response.status_code == 409


async def test_skip_rejects_a_question_from_another_session(api, registered_user):
    headers = registered_user["headers"]
    first = await _session(api, headers)
    second = await _session(api, headers)
    foreign = (await _questions(api, headers, second["id"]))[0]

    response = await api.post(
        f"/api/v1/interviews/{first['id']}/questions/{foreign['id']}/skip", headers=headers
    )

    assert response.status_code == 404


async def test_another_user_cannot_skip(api, registered_user, other_user):
    session = await _session(api, registered_user["headers"])
    question = (await _questions(api, registered_user["headers"], session["id"]))[0]

    response = await api.post(
        f"/api/v1/interviews/{session['id']}/questions/{question['id']}/skip",
        headers=other_user["headers"],
    )

    assert response.status_code == 404


# -- Re-answer -----------------------------------------------------------------


async def test_update_replaces_the_answer(api, registered_user):
    headers = registered_user["headers"]
    session = await _session(api, headers)
    question = (await _questions(api, headers, session["id"]))[0]
    await api.post(
        f"/api/v1/interviews/{session['id']}/answers",
        json={"question_id": question["id"], "content": "First attempt.", "duration_seconds": 10},
        headers=headers,
    )

    response = await api.put(
        f"/api/v1/interviews/{session['id']}/answers",
        json={"question_id": question["id"], "content": "Better attempt.", "duration_seconds": 42},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["content"] == "Better attempt."
    assert response.json()["duration_seconds"] == 42

    refreshed = (await _questions(api, headers, session["id"]))[0]
    assert refreshed["answer"]["content"] == "Better attempt."


async def test_update_does_not_create_a_second_answer(api, registered_user):
    headers = registered_user["headers"]
    session = await _session(api, headers)
    question = (await _questions(api, headers, session["id"]))[0]
    body = {"question_id": question["id"], "content": "One."}
    await api.post(f"/api/v1/interviews/{session['id']}/answers", json=body, headers=headers)

    await api.put(
        f"/api/v1/interviews/{session['id']}/answers",
        json={**body, "content": "Two."},
        headers=headers,
    )

    # 1:1 with the question; a second row would violate that and orphan one.
    questions = await _questions(api, headers, session["id"])
    assert sum(1 for q in questions if q["answer"] is not None) == 1


async def test_update_requires_an_existing_answer(api, registered_user):
    headers = registered_user["headers"]
    session = await _session(api, headers)
    question = (await _questions(api, headers, session["id"]))[0]

    response = await api.put(
        f"/api/v1/interviews/{session['id']}/answers",
        json={"question_id": question["id"], "content": "Nothing to replace."},
        headers=headers,
    )

    assert response.status_code == 409


async def test_update_rejects_an_empty_answer(api, registered_user):
    headers = registered_user["headers"]
    session = await _session(api, headers)
    question = (await _questions(api, headers, session["id"]))[0]
    await api.post(
        f"/api/v1/interviews/{session['id']}/answers",
        json={"question_id": question["id"], "content": "Real answer."},
        headers=headers,
    )

    response = await api.put(
        f"/api/v1/interviews/{session['id']}/answers",
        json={"question_id": question["id"], "content": ""},
        headers=headers,
    )

    assert response.status_code == 422


async def test_another_user_cannot_update_an_answer(api, registered_user, other_user):
    headers = registered_user["headers"]
    session = await _session(api, headers)
    question = (await _questions(api, headers, session["id"]))[0]
    await api.post(
        f"/api/v1/interviews/{session['id']}/answers",
        json={"question_id": question["id"], "content": "Mine."},
        headers=headers,
    )

    response = await api.put(
        f"/api/v1/interviews/{session['id']}/answers",
        json={"question_id": question["id"], "content": "Not yours."},
        headers=other_user["headers"],
    )

    assert response.status_code == 404
    unchanged = (await _questions(api, headers, session["id"]))[0]
    assert unchanged["answer"]["content"] == "Mine."


# -- Regenerate ----------------------------------------------------------------


async def test_regenerate_replaces_the_question_set(api, registered_user):
    headers = registered_user["headers"]
    session = await _session(api, headers, question_count=4)
    original_ids = {q["id"] for q in await _questions(api, headers, session["id"])}

    response = await api.post(
        f"/api/v1/interviews/{session['id']}/regenerate-questions", headers=headers
    )

    assert response.status_code == 200
    fresh = response.json()["questions"]
    assert len(fresh) == 4
    # A genuinely new set, not the old rows returned again.
    assert {q["id"] for q in fresh}.isdisjoint(original_ids)
    # Numbering restarts, so the UI's "question N of M" stays honest.
    assert [q["sequence_number"] for q in fresh] == [1, 2, 3, 4]


async def test_regenerate_leaves_no_orphaned_questions(api, registered_user):
    headers = registered_user["headers"]
    session = await _session(api, headers, question_count=3)

    await api.post(f"/api/v1/interviews/{session['id']}/regenerate-questions", headers=headers)

    # Exactly the new set -- the old questions are deleted, not hidden.
    assert len(await _questions(api, headers, session["id"])) == 3


async def test_regenerate_is_refused_once_anything_is_answered(api, registered_user):
    """Discarding the questions would discard the answers with them."""
    headers = registered_user["headers"]
    session = await _session(api, headers)
    question = (await _questions(api, headers, session["id"]))[0]
    await api.post(
        f"/api/v1/interviews/{session['id']}/answers",
        json={"question_id": question["id"], "content": "Work I do not want to lose."},
        headers=headers,
    )

    response = await api.post(
        f"/api/v1/interviews/{session['id']}/regenerate-questions", headers=headers
    )

    assert response.status_code == 409
    # And the answer is still there.
    kept = (await _questions(api, headers, session["id"]))[0]
    assert kept["answer"]["content"] == "Work I do not want to lose."


async def test_regenerate_requires_an_in_progress_session(api, registered_user):
    headers = registered_user["headers"]
    session = await _session(api, headers)
    await api.post(f"/api/v1/interviews/{session['id']}/abandon", headers=headers)

    response = await api.post(
        f"/api/v1/interviews/{session['id']}/regenerate-questions", headers=headers
    )

    assert response.status_code == 409


async def test_another_user_cannot_regenerate(api, registered_user, other_user):
    session = await _session(api, registered_user["headers"])

    response = await api.post(
        f"/api/v1/interviews/{session['id']}/regenerate-questions",
        headers=other_user["headers"],
    )

    assert response.status_code == 404
