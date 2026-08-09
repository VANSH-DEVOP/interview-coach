"""Refresh-token rotation, revocation and reuse detection.

These run against a real database because the whole feature *is* server-side
state. A fake repository would prove the service calls the methods, not that a
revoked token actually stops working.
"""

import uuid

import pytest
from sqlalchemy import select

from app.core.security import decode_token
from app.models.refresh_token import RefreshToken


async def _tokens(api, user) -> dict:
    response = await api.post(
        "/api/v1/auth/login", json={"email": user["email"], "password": user["password"]}
    )
    assert response.status_code == 200, response.text
    return response.json()


# -- Rotation ------------------------------------------------------------------


async def test_login_records_the_refresh_token(api, registered_user, db_session):
    """A token the server has no row for can never be refreshed."""
    tokens = await _tokens(api, registered_user)

    jti = decode_token(tokens["refresh_token"], expected_type="refresh")["jti"]
    record = (
        await db_session.execute(select(RefreshToken).where(RefreshToken.jti == jti))
    ).scalar_one()
    assert record.revoked_at is None
    assert str(record.user_id) == registered_user["user"]["id"]


async def test_refresh_returns_a_different_refresh_token(api, registered_user):
    tokens = await _tokens(api, registered_user)

    response = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )

    assert response.status_code == 200
    assert response.json()["refresh_token"] != tokens["refresh_token"]


async def test_the_rotated_token_stops_working(api, registered_user):
    """Without this, rotation is decoration -- the old token stays valid for
    days and stealing it is as good as stealing the account."""
    tokens = await _tokens(api, registered_user)
    await api.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})

    again = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )

    assert again.status_code == 401


async def test_the_new_token_works(api, registered_user):
    tokens = await _tokens(api, registered_user)
    rotated = (
        await api.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    ).json()

    response = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": rotated["refresh_token"]}
    )

    assert response.status_code == 200


# -- Reuse detection -----------------------------------------------------------


async def test_reusing_a_rotated_token_revokes_the_whole_account(api, registered_user):
    """The legitimate client already rotated this token away, so whoever is
    presenting it replayed a captured one. Every session is dropped; an attacker
    without the password cannot get back in, and the real user logs in again."""
    first = await _tokens(api, registered_user)
    second = await _tokens(api, registered_user)  # a second device
    rotated = (
        await api.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    ).json()

    # Replay the token that was rotated away.
    replay = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )
    assert replay.status_code == 401

    # The other device and the freshly rotated token are both cut off too.
    for token in (second["refresh_token"], rotated["refresh_token"]):
        response = await api.post("/api/v1/auth/refresh", json={"refresh_token": token})
        assert response.status_code == 401, "reuse detection did not revoke the family"


async def test_the_password_still_works_after_a_reuse_lockout(api, registered_user):
    """Revoking sessions must not lock the real user out of their account."""
    tokens = await _tokens(api, registered_user)
    await api.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    await api.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})

    assert (await _tokens(api, registered_user))["access_token"]


# -- Rejection cases -----------------------------------------------------------


async def test_a_garbage_token_is_rejected(api):
    response = await api.post("/api/v1/auth/refresh", json={"refresh_token": "not-a-jwt"})
    assert response.status_code == 401


async def test_an_access_token_cannot_be_used_to_refresh(api, registered_user):
    tokens = await _tokens(api, registered_user)
    response = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["access_token"]}
    )
    assert response.status_code == 401


async def test_a_validly_signed_token_with_no_record_is_rejected(api, registered_user):
    """Covers tokens issued before the revocation list existed, and rows pruned
    after expiry. The signature is genuine; the server still says no."""
    from app.core.security import create_refresh_token

    orphan = create_refresh_token(registered_user["user"]["id"])

    response = await api.post("/api/v1/auth/refresh", json={"refresh_token": orphan.token})

    assert response.status_code == 401


async def test_a_token_for_a_deleted_user_is_rejected(api, registered_user, db_session):
    from app.models.user import User

    tokens = await _tokens(api, registered_user)
    user = await db_session.get(User, uuid.UUID(registered_user["user"]["id"]))
    user.is_active = False
    await db_session.flush()

    response = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )

    assert response.status_code == 401


# -- Logout --------------------------------------------------------------------


async def test_logout_revokes_the_token(api, registered_user):
    tokens = await _tokens(api, registered_user)

    logout = await api.post(
        "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}
    )
    assert logout.status_code == 204

    response = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 401


async def test_logout_leaves_other_sessions_alone(api, registered_user):
    """Signing out of a laptop must not sign you out of your phone."""
    laptop = await _tokens(api, registered_user)
    phone = await _tokens(api, registered_user)

    await api.post("/api/v1/auth/logout", json={"refresh_token": laptop["refresh_token"]})

    response = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": phone["refresh_token"]}
    )
    assert response.status_code == 200


async def test_logout_everywhere_revokes_every_session(api, registered_user):
    laptop = await _tokens(api, registered_user)
    phone = await _tokens(api, registered_user)

    await api.post(
        "/api/v1/auth/logout",
        json={"refresh_token": laptop["refresh_token"], "everywhere": True},
    )

    for token in (laptop["refresh_token"], phone["refresh_token"]):
        response = await api.post("/api/v1/auth/refresh", json={"refresh_token": token})
        assert response.status_code == 401


@pytest.mark.parametrize("token", ["not-a-jwt", ""])
async def test_logout_is_quiet_about_invalid_tokens(api, token):
    """The client wanted to be signed out. It now is. Reporting an error only
    teaches clients to ignore the response."""
    response = await api.post("/api/v1/auth/logout", json={"refresh_token": token})
    assert response.status_code == 204


async def test_logout_twice_is_not_an_error(api, registered_user):
    tokens = await _tokens(api, registered_user)
    body = {"refresh_token": tokens["refresh_token"]}

    assert (await api.post("/api/v1/auth/logout", json=body)).status_code == 204
    assert (await api.post("/api/v1/auth/logout", json=body)).status_code == 204


async def test_logout_does_not_require_an_access_token(api, registered_user):
    """Logging out is what a client does when its access token has expired."""
    tokens = await _tokens(api, registered_user)

    response = await api.post(
        "/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]}
    )

    assert response.status_code == 204
