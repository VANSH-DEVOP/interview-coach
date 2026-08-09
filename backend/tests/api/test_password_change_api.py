"""Changing a password.

The security-relevant behaviour is not the hash swap -- it is what happens to
every other session, and to the one making the request.
"""

import pytest


async def _login(api, email, password):
    return await api.post("/api/v1/auth/login", json={"email": email, "password": password})


async def test_the_new_password_works(api, registered_user):
    response = await api.post(
        "/api/v1/users/me/password",
        json={"current_password": registered_user["password"], "new_password": "a-brand-new-one"},
        headers=registered_user["headers"],
    )
    assert response.status_code == 200

    assert (await _login(api, registered_user["email"], "a-brand-new-one")).status_code == 200


async def test_the_old_password_stops_working(api, registered_user):
    await api.post(
        "/api/v1/users/me/password",
        json={"current_password": registered_user["password"], "new_password": "a-brand-new-one"},
        headers=registered_user["headers"],
    )

    response = await _login(api, registered_user["email"], registered_user["password"])
    assert response.status_code == 401


async def test_other_sessions_are_signed_out(api, registered_user):
    """Most of the point: the usual reason to change a password is believing
    someone else has a session."""
    other = (await _login(api, registered_user["email"], registered_user["password"])).json()

    await api.post(
        "/api/v1/users/me/password",
        json={"current_password": registered_user["password"], "new_password": "a-brand-new-one"},
        headers=registered_user["headers"],
    )

    refreshed = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": other["refresh_token"]}
    )
    assert refreshed.status_code == 401


async def test_the_caller_gets_a_working_pair_back(api, registered_user):
    """Revoking everything would sign out the device doing the change."""
    response = await api.post(
        "/api/v1/users/me/password",
        json={"current_password": registered_user["password"], "new_password": "a-brand-new-one"},
        headers=registered_user["headers"],
    )

    pair = response.json()
    assert pair["access_token"] and pair["refresh_token"]
    # The returned refresh token is live, not one of the ones just revoked.
    rotated = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert rotated.status_code == 200


async def test_the_callers_previous_refresh_token_is_dead(api, registered_user):
    previous = registered_user["tokens"]["refresh_token"]

    await api.post(
        "/api/v1/users/me/password",
        json={"current_password": registered_user["password"], "new_password": "a-brand-new-one"},
        headers=registered_user["headers"],
    )

    response = await api.post("/api/v1/auth/refresh", json={"refresh_token": previous})
    assert response.status_code == 401


# -- Rejections ----------------------------------------------------------------


async def test_a_wrong_current_password_is_rejected(api, registered_user):
    """An access token left on a shared machine must not be enough to take the
    account over permanently."""
    response = await api.post(
        "/api/v1/users/me/password",
        json={"current_password": "not-the-password", "new_password": "a-brand-new-one"},
        headers=registered_user["headers"],
    )

    assert response.status_code == 401
    assert (await _login(api, registered_user["email"], registered_user["password"])).status_code == 200


async def test_reusing_the_same_password_is_rejected(api, registered_user):
    response = await api.post(
        "/api/v1/users/me/password",
        json={
            "current_password": registered_user["password"],
            "new_password": registered_user["password"],
        },
        headers=registered_user["headers"],
    )
    assert response.status_code == 422


@pytest.mark.parametrize("new_password", ["short", ""])
async def test_a_weak_new_password_is_rejected(api, registered_user, new_password):
    response = await api.post(
        "/api/v1/users/me/password",
        json={"current_password": registered_user["password"], "new_password": new_password},
        headers=registered_user["headers"],
    )
    assert response.status_code == 422


async def test_authentication_is_required(api):
    response = await api.post(
        "/api/v1/users/me/password",
        json={"current_password": "x", "new_password": "a-brand-new-one"},
    )
    assert response.status_code == 401


async def test_a_failed_attempt_does_not_revoke_sessions(api, registered_user):
    """A wrong guess must not become a denial-of-service against the account."""
    other = (await _login(api, registered_user["email"], registered_user["password"])).json()

    await api.post(
        "/api/v1/users/me/password",
        json={"current_password": "wrong", "new_password": "a-brand-new-one"},
        headers=registered_user["headers"],
    )

    refreshed = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": other["refresh_token"]}
    )
    assert refreshed.status_code == 200
