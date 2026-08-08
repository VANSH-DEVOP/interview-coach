"""Rate limiting: window mechanics and HTTP enforcement.

The AI limits exist because the Gemini free tier allows 20 requests/day for
the whole deployment -- without them one user drains the shared quota and
every other user silently receives deterministic fallback output.
"""

import pytest

from app.api.deps import get_auth_service
from app.core import rate_limit
from app.core.config import get_settings
from app.core.exceptions import RateLimitedError, UnauthorizedError
from app.main import app


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

    rate_limit._enforce("1.2.3.4", scope="auth")
    rate_limit._enforce("1.2.3.4", scope="auth")

    with pytest.raises(RateLimitedError) as excinfo:
        rate_limit._enforce("1.2.3.4", scope="auth")

    exc = excinfo.value
    assert exc.status_code == 429
    assert exc.code == "rate_limited"
    assert int(exc.headers["Retry-After"]) > 0


def test_enforce_is_a_no_op_when_disabled(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False, raising=False)
    monkeypatch.setattr(settings, "RATE_LIMIT_AUTH_ATTEMPTS", 1, raising=False)

    for _ in range(50):
        rate_limit._enforce("1.2.3.4", scope="auth")


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

    rate_limit._enforce("same-key", scope="auth")
    with pytest.raises(RateLimitedError):
        rate_limit._enforce("same-key", scope="auth")

    # Exhausting auth must not consume the AI budget, even for the same key.
    rate_limit._enforce("same-key", scope="ai")


async def test_ai_routes_reject_unauthenticated_before_consuming_a_slot(client):
    # limit_by_user depends on get_current_user, so an anonymous caller gets
    # 401 and cannot burn another user's quota or fill the table.
    response = await client.post("/api/v1/interviews", json={"title": "x"})
    assert response.status_code == 401
