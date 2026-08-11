"""Per-user quotas, and why the two of them are enforced differently.

There are two kinds of limit here and they are not the same shape:

- **Occupancy** -- how much an account *holds*. Resumes. Enforced by counting
  rows, so a Redis restart cannot grant unlimited uploads and deleting a resume
  frees the quota immediately, which is right: the resource is storage, and
  deleting returns it.
- **Consumption** -- what an account *spends*. Interviews. Enforced by a window
  counter, because a provider call cannot be un-spent. Counting rows here would
  make delete-and-retry a way round the cap.

Both of those claims are asserted below rather than left in a comment, because
each is exactly the sort of thing that looks like an implementation detail right
up until someone "simplifies" the two into one mechanism.
"""

import io
import uuid

import pytest
from pypdf import PdfWriter

from app.core.config import get_settings

PDF_TYPE = "application/pdf"


def _pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


async def _upload(api, headers, name="cv.pdf"):
    return await api.post(
        "/api/v1/resumes",
        files={"file": (name, _pdf_bytes(), PDF_TYPE)},
        headers=headers,
    )


@pytest.fixture
def auth_headers(registered_user):
    return registered_user["headers"]


@pytest.fixture
async def second_account(api):
    email = f"second-{uuid.uuid4().hex[:8]}@example.com"
    password = "correct-horse-battery"
    await api.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Second"},
    )
    login = await api.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest.fixture
def resume_limit(monkeypatch):
    def _set(value):
        monkeypatch.setattr(get_settings(), "MAX_RESUMES_PER_USER", value, raising=False)

    return _set


# -- The resume quota (occupancy) -----------------------------------------------


async def test_uploads_are_refused_once_the_account_is_full(
    api, auth_headers, resume_limit, storage_root
):
    resume_limit(2)

    assert (await _upload(api, auth_headers)).status_code == 201
    assert (await _upload(api, auth_headers)).status_code == 201
    response = await _upload(api, auth_headers)

    assert response.status_code == 429
    error = response.json()["error"]
    # Distinguishable from a rate limit, because the way out is different: one
    # clears by waiting and the other by deleting.
    assert error["code"] == "quota_exceeded"
    assert error["details"] == {"limit": 2, "current": 2, "resource": "resumes"}
    # No Retry-After: waiting does not help, and a time here would be a lie.
    assert "retry-after" not in response.headers


async def test_deleting_a_resume_frees_the_quota(
    api, auth_headers, resume_limit, storage_root
):
    """The behaviour that justifies counting rows instead of keeping a counter.

    A counter would have to be decremented on delete -- a second place to get
    wrong -- and would still be wrong after a Redis restart.
    """
    resume_limit(1)
    first = await _upload(api, auth_headers)
    assert (await _upload(api, auth_headers)).status_code == 429

    deleted = await api.delete(
        f"/api/v1/resumes/{first.json()['id']}", headers=auth_headers
    )
    assert deleted.status_code == 204

    assert (await _upload(api, auth_headers)).status_code == 201


async def test_the_quota_is_per_account(
    api, auth_headers, second_account, resume_limit, storage_root
):
    resume_limit(1)
    assert (await _upload(api, auth_headers)).status_code == 201
    assert (await _upload(api, auth_headers)).status_code == 429

    # A full account must not spend anyone else's allowance.
    assert (await _upload(api, second_account)).status_code == 201


@pytest.mark.parametrize("value", [0, -1])
async def test_zero_or_less_means_unlimited(
    api, auth_headers, resume_limit, storage_root, value
):
    """`0` is the obvious guess for "switch the quota off", and before this it
    meant `held >= 0` -- every upload rejected. An optional int was no better:
    `null` does not validate through the environment and a blank value falls
    back to the default, so there was no way to turn the quota off at all."""
    resume_limit(value)

    for _ in range(4):
        assert (await _upload(api, auth_headers)).status_code == 201


async def test_a_refused_upload_leaves_no_file_behind(
    api, auth_headers, resume_limit, storage_root
):
    """The quota is checked before the blob is written, not after the row is
    built: a storage write is the side effect that outlives a failed request."""
    resume_limit(1)
    await _upload(api, auth_headers)

    def stored_files():
        return [path for path in storage_root.rglob("*") if path.is_file()]

    before = len(stored_files())
    assert (await _upload(api, auth_headers)).status_code == 429

    assert len(stored_files()) == before


# -- The interview cap (consumption) --------------------------------------------


@pytest.fixture
def interview_cap(monkeypatch):
    """Rate limiting is off for API tests by default; this cap is one of the few
    places where the limiter *is* the feature under test."""
    settings = get_settings()
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "RATE_LIMIT_INTERVIEW_CREATES", 2, raising=False)
    monkeypatch.setattr(
        settings, "RATE_LIMIT_INTERVIEW_WINDOW_SECONDS", 86400, raising=False
    )
    # The hourly burst guard must not be what stops the test.
    monkeypatch.setattr(settings, "RATE_LIMIT_AI_REQUESTS", 1000, raising=False)

    from app.core import rate_limit

    rate_limit.reset()
    yield
    rate_limit.reset()


async def _create_interview(api, headers):
    return await api.post(
        "/api/v1/interviews",
        json={"title": "Practice", "target_role": "Backend Engineer"},
        headers=headers,
    )


async def test_a_daily_cap_stops_interview_creation(api, auth_headers, interview_cap):
    assert (await _create_interview(api, auth_headers)).status_code == 201
    assert (await _create_interview(api, auth_headers)).status_code == 201

    response = await _create_interview(api, auth_headers)

    assert response.status_code == 429
    # Waiting *is* the answer here, so unlike the resume quota this one says
    # how long.
    assert "retry-after" in response.headers


async def test_deleting_an_interview_does_not_refund_the_cap(
    api, auth_headers, interview_cap
):
    """The reason this cap is a counter and not a count of rows.

    Starting an interview spends a provider call against an account ceiling of
    twenty a day. Deleting the session cannot un-spend it, so delete-and-retry
    must not be a way round the limit.
    """
    first = await _create_interview(api, auth_headers)
    await _create_interview(api, auth_headers)

    deleted = await api.delete(
        f"/api/v1/interviews/{first.json()['id']}", headers=auth_headers
    )
    assert deleted.status_code == 204

    assert (await _create_interview(api, auth_headers)).status_code == 429


async def test_reading_interviews_is_never_capped(api, auth_headers, interview_cap):
    """The cap is on creation. A user who has hit it must still be able to see
    and finish the interviews they already have."""
    await _create_interview(api, auth_headers)
    await _create_interview(api, auth_headers)
    assert (await _create_interview(api, auth_headers)).status_code == 429

    assert (await api.get("/api/v1/interviews", headers=auth_headers)).status_code == 200
