"""Rate limiting: window mechanics and HTTP enforcement.

The AI limits exist because the Gemini free tier allows 20 requests/day for
the whole deployment -- without them one user drains the shared quota and
every other user silently receives deterministic fallback output.
"""

import uuid
from datetime import datetime

import pytest

from app.api.deps import get_auth_service, get_current_user, get_interview_service
from app.core import rate_limit
from app.core.config import get_settings
from app.core.exceptions import RateLimitedError, UnauthorizedError
from app.main import app
from app.models.interview_session import InterviewSession, SessionStatus


@pytest.fixture(autouse=True)
def _clean_state():
    rate_limit.reset()
    yield
    rate_limit.reset()
    get_settings.cache_clear()


class _RejectingAuthService:
    """Auth service that always rejects, without touching a database.

    The limiter runs before the route body, so the outcome of the underlying
    call is irrelevant -- what matters is that failed attempts are counted.
    Stubbing it keeps these tests independent of Postgres.
    """

    async def login(self, email, password):
        raise UnauthorizedError("Invalid credentials.")


@pytest.fixture
def stub_auth():
    app.dependency_overrides[get_auth_service] = lambda: _RejectingAuthService()
    yield
    app.dependency_overrides.pop(get_auth_service, None)


class _FakeUser:
    def __init__(self) -> None:
        self.id = uuid.uuid4()
        self.is_active = True


class _StubInterviewService:
    """Returns a valid session without a database or an AI provider."""

    async def create(self, user_id, payload):
        return InterviewSession(
            id=uuid.uuid4(),
            user_id=user_id,
            resume_id=None,
            title=payload.title,
            target_role=payload.target_role,
            status=SessionStatus.IN_PROGRESS,
            interview_type=payload.interview_type,
            difficulty=payload.difficulty,
            question_count=payload.question_count,
            started_at=datetime(2026, 1, 1),
            completed_at=None,
            created_at=datetime(2026, 1, 1),
        )


@pytest.fixture
def stub_user():
    # One user for the whole test: a fresh one per request would land in a
    # different bucket and never hit the limit.
    user = _FakeUser()
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def stub_interviews():
    app.dependency_overrides[get_interview_service] = lambda: _StubInterviewService()
    yield
    app.dependency_overrides.pop(get_interview_service, None)


# -- _Window ------------------------------------------------------------------


def test_window_allows_up_to_the_limit():
    window = rate_limit._Window()
    for _ in range(5):
        assert window.hit("k", limit=5, window_seconds=60) is None


def test_window_blocks_past_the_limit_and_reports_retry_after():
    window = rate_limit._Window()
    for _ in range(3):
        window.hit("k", limit=3, window_seconds=60)

    retry_after = window.hit("k", limit=3, window_seconds=60)
    assert retry_after is not None
    assert 0 < retry_after <= 60


def test_window_keys_are_independent():
    window = rate_limit._Window()
    for _ in range(3):
        window.hit("a", limit=3, window_seconds=60)

    # "a" is exhausted; "b" is untouched.
    assert window.hit("a", limit=3, window_seconds=60) is not None
    assert window.hit("b", limit=3, window_seconds=60) is None


def test_window_resets_after_expiry():
    window = rate_limit._Window()
    # A zero-length window has always expired by the next call.
    assert window.hit("k", limit=1, window_seconds=0) is None
    assert window.hit("k", limit=1, window_seconds=0) is None


def test_window_prunes_expired_keys():
    window = rate_limit._Window()
    for i in range(rate_limit._PRUNE_THRESHOLD + 1):
        window.hit(f"k{i}", limit=1, window_seconds=0)
    # Expired entries are dropped rather than accumulating forever.
    assert len(window._hits) < rate_limit._PRUNE_THRESHOLD


# -- _enforce -----------------------------------------------------------------


def test_enforce_raises_with_a_retry_after_header(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "RATE_LIMIT_AUTH_ATTEMPTS", 2, raising=False)
    monkeypatch.setattr(settings, "RATE_LIMIT_AUTH_WINDOW_SECONDS", 60, raising=False)

    rate_limit.enforce("1.2.3.4", scope="auth")
    rate_limit.enforce("1.2.3.4", scope="auth")

    with pytest.raises(RateLimitedError) as excinfo:
        rate_limit.enforce("1.2.3.4", scope="auth")

    exc = excinfo.value
    assert exc.status_code == 429
    assert exc.code == "rate_limited"
    assert int(exc.headers["Retry-After"]) > 0


def test_enforce_is_a_no_op_when_disabled(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False, raising=False)
    monkeypatch.setattr(settings, "RATE_LIMIT_AUTH_ATTEMPTS", 1, raising=False)

    for _ in range(50):
        rate_limit.enforce("1.2.3.4", scope="auth")


# -- HTTP ---------------------------------------------------------------------


async def test_auth_endpoint_returns_429_in_the_error_envelope(
    client, stub_auth, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "RATE_LIMIT_AUTH_ATTEMPTS", 3, raising=False)
    monkeypatch.setattr(settings, "RATE_LIMIT_AUTH_WINDOW_SECONDS", 300, raising=False)

    body = {"email": "nobody@example.com", "password": "whatever123"}
    for _ in range(3):
        assert (await client.post("/api/v1/auth/login", json=body)).status_code == 401

    response = await client.post("/api/v1/auth/login", json=body)

    assert response.status_code == 429
    assert response.headers["Retry-After"]
    # Same envelope shape as every other error, so the frontend ApiError
    # parser needs no special case.
    assert response.json()["error"]["code"] == "rate_limited"


async def test_failed_attempts_are_counted_not_just_successful_ones(
    client, stub_auth, monkeypatch
):
    # The point of an auth limiter is to bound *guessing*, so rejected
    # credentials must consume the budget.
    settings = get_settings()
    monkeypatch.setattr(settings, "RATE_LIMIT_AUTH_ATTEMPTS", 2, raising=False)

    body = {"email": "nobody@example.com", "password": "whatever123"}
    await client.post("/api/v1/auth/login", json=body)
    await client.post("/api/v1/auth/login", json=body)

    assert (await client.post("/api/v1/auth/login", json=body)).status_code == 429


def test_scopes_are_independent(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "RATE_LIMIT_AUTH_ATTEMPTS", 1, raising=False)
    monkeypatch.setattr(settings, "RATE_LIMIT_AI_REQUESTS", 5, raising=False)

    rate_limit.enforce("same-key", scope="auth")
    with pytest.raises(RateLimitedError):
        rate_limit.enforce("same-key", scope="auth")

    # Exhausting auth must not consume the AI budget, even for the same key.
    rate_limit.enforce("same-key", scope="ai")


async def test_ai_routes_reject_unauthenticated_before_consuming_a_slot(client):
    # limit_by_user depends on get_current_user, so an anonymous caller gets
    # 401 and cannot burn another user's quota or fill the table.
    response = await client.post("/api/v1/interviews", json={"title": "x"})
    assert response.status_code == 401


async def test_authenticated_ai_route_passes_the_limiter_and_then_429s(
    client, stub_user, stub_interviews, monkeypatch
):
    """Regression guard for a limiter that broke the route it protected.

    limit_by_user originally lived in app/core/rate_limit.py, which has
    `from __future__ import annotations`. Its inner dependency imported User
    locally, so FastAPI could not resolve `Annotated[User, Depends(...)]`,
    silently reinterpreted `user` as a *query parameter*, and every AI-backed
    route answered 422 "Field required: query.user".

    The 401 test above passed throughout, because auth failed before the
    broken annotation mattered. Only an authenticated request catches it.
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "RATE_LIMIT_AI_REQUESTS", 2, raising=False)

    body = {"title": "Practice"}
    for _ in range(2):
        response = await client.post("/api/v1/interviews", json=body)
        assert response.status_code == 201, response.text

    limited = await client.post("/api/v1/interviews", json=body)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"


async def test_ai_budget_is_per_user(client, stub_interviews, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "RATE_LIMIT_AI_REQUESTS", 1, raising=False)

    first, second = _FakeUser(), _FakeUser()

    app.dependency_overrides[get_current_user] = lambda: first
    assert (await client.post("/api/v1/interviews", json={"title": "a"})).status_code == 201
    assert (await client.post("/api/v1/interviews", json={"title": "a"})).status_code == 429

    # A different user still has their full budget.
    app.dependency_overrides[get_current_user] = lambda: second
    assert (await client.post("/api/v1/interviews", json={"title": "a"})).status_code == 201

    app.dependency_overrides.pop(get_current_user, None)
