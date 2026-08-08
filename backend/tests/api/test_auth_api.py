"""Auth and user endpoints over HTTP, against a real database."""

import uuid

import pytest

CREDENTIALS = {
    "email": "new-user@example.com",
    "password": "correct-horse-battery",
    "full_name": "New User",
}


def _unique(email_prefix: str) -> dict:
    return {**CREDENTIALS, "email": f"{email_prefix}-{uuid.uuid4().hex[:8]}@example.com"}


# -- Registration --------------------------------------------------------------


async def test_register_creates_a_user(api):
    response = await api.post("/api/v1/auth/register", json=_unique("reg"))

    assert response.status_code == 201
    body = response.json()
    assert body["is_active"] is True
    assert "id" in body


async def test_register_never_returns_the_password(api):
    payload = _unique("leak")
    response = await api.post("/api/v1/auth/register", json=payload)

    serialised = response.text
    assert payload["password"] not in serialised
    assert "hashed_password" not in serialised
    assert "password" not in response.json()


async def test_register_rejects_a_duplicate_email(api):
    payload = _unique("dupe")
    assert (await api.post("/api/v1/auth/register", json=payload)).status_code == 201

    response = await api.post("/api/v1/auth/register", json=payload)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


@pytest.mark.parametrize(
    "override",
    [
        {"email": "not-an-email"},
        {"password": "short"},
        {"email": ""},
    ],
)
async def test_register_validates_input(api, override):
    response = await api.post("/api/v1/auth/register", json={**_unique("bad"), **override})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# -- Login ---------------------------------------------------------------------


async def test_login_returns_a_token_pair(api, registered_user):
    response = await api.post(
        "/api/v1/auth/login",
        json={"email": registered_user["email"], "password": registered_user["password"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"] and body["refresh_token"]
    assert body["access_token"] != body["refresh_token"]


async def test_login_rejects_a_wrong_password(api, registered_user):
    response = await api.post(
        "/api/v1/auth/login",
        json={"email": registered_user["email"], "password": "wrong-password"},
    )

    assert response.status_code == 401


async def test_login_rejects_an_unknown_email_the_same_way(api, registered_user):
    """A different response for unknown vs wrong-password would enumerate users."""
    unknown = await api.post(
        "/api/v1/auth/login",
        json={"email": "nobody-here@example.com", "password": "correct-horse-battery"},
    )
    wrong_password = await api.post(
        "/api/v1/auth/login",
        json={"email": registered_user["email"], "password": "wrong-password"},
    )

    assert unknown.status_code == wrong_password.status_code == 401
    assert unknown.json()["error"]["message"] == wrong_password.json()["error"]["message"]


# -- Refresh -------------------------------------------------------------------


async def test_refresh_issues_a_new_access_token(api, registered_user):
    response = await api.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": registered_user["tokens"]["refresh_token"]},
    )

    assert response.status_code == 200
    assert response.json()["access_token"]


async def test_refresh_rejects_an_access_token(api, registered_user):
    """Token type is checked, so an access token cannot be used to refresh."""
    response = await api.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": registered_user["tokens"]["access_token"]},
    )

    assert response.status_code == 401


async def test_refresh_rejects_garbage(api):
    response = await api.post("/api/v1/auth/refresh", json={"refresh_token": "nonsense"})

    assert response.status_code == 401


# -- Current user --------------------------------------------------------------


async def test_me_requires_authentication(api):
    assert (await api.get("/api/v1/users/me")).status_code == 401


async def test_me_rejects_a_malformed_token(api):
    response = await api.get(
        "/api/v1/users/me", headers={"Authorization": "Bearer not.a.jwt"}
    )
    assert response.status_code == 401


async def test_me_rejects_a_refresh_token(api, registered_user):
    """An access endpoint must not accept the longer-lived refresh token."""
    response = await api.get(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {registered_user['tokens']['refresh_token']}"},
    )
    assert response.status_code == 401


async def test_me_returns_the_authenticated_user(api, registered_user):
    response = await api.get("/api/v1/users/me", headers=registered_user["headers"])

    assert response.status_code == 200
    assert response.json()["email"] == registered_user["email"]


async def test_patch_me_updates_the_profile(api, registered_user):
    response = await api.patch(
        "/api/v1/users/me",
        json={"full_name": "Renamed Person"},
        headers=registered_user["headers"],
    )

    assert response.status_code == 200
    assert response.json()["full_name"] == "Renamed Person"

    # Persisted, not just echoed back.
    again = await api.get("/api/v1/users/me", headers=registered_user["headers"])
    assert again.json()["full_name"] == "Renamed Person"


async def test_patch_me_requires_authentication(api):
    response = await api.patch("/api/v1/users/me", json={"full_name": "Nobody"})
    assert response.status_code == 401
