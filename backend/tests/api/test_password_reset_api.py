"""Forgot / reset password.

Two properties carry the weight here: the flow must not tell an anonymous
caller whether an address has an account, and completing a reset must end every
session that existed before it.
"""

import pytest

from app.models.one_time_token import TokenPurpose
from tests.conftest import token_from


@pytest.fixture
async def user_with_mailbox(api, mailbox, registered_user):
    """A registered user, with the mailbox cleared of the signup email."""
    mailbox.clear()
    return registered_user


async def _forgot(api, email):
    return await api.post("/api/v1/auth/forgot-password", json={"email": email})


# -- Not leaking who has an account --------------------------------------------


async def test_a_known_address_returns_202(api, user_with_mailbox):
    assert (await _forgot(api, user_with_mailbox["email"])).status_code == 202


async def test_an_unknown_address_returns_202_too(api, mailbox):
    """The whole point. A 404 here turns a leaked address list into a
    membership check against this service."""
    response = await _forgot(api, "nobody-here@example.com")

    assert response.status_code == 202
    assert response.content in (b"", b"null")
    assert mailbox.sent == [], "an email was sent to an address with no account"


async def test_the_responses_are_indistinguishable(api, user_with_mailbox):
    known = await _forgot(api, user_with_mailbox["email"])
    unknown = await _forgot(api, "nobody-here@example.com")

    assert known.status_code == unknown.status_code
    assert known.content == unknown.content


async def test_a_deactivated_account_gets_nothing(api, mailbox, registered_user, db_session):
    import uuid

    from app.models.user import User

    user = await db_session.get(User, uuid.UUID(registered_user["user"]["id"]))
    user.is_active = False
    await db_session.flush()
    mailbox.clear()

    assert (await _forgot(api, registered_user["email"])).status_code == 202
    assert mailbox.sent == []


# -- The happy path ------------------------------------------------------------


async def test_the_email_goes_to_the_right_address_with_a_link(api, mailbox, user_with_mailbox):
    await _forgot(api, user_with_mailbox["email"])

    message = mailbox.last_to(user_with_mailbox["email"])
    assert message is not None
    assert "reset" in message.subject.lower()
    # Absolute, and built from FRONTEND_BASE_URL rather than the request Host,
    # which an attacker controls.
    assert "http://localhost:3000/reset-password?token=" in message.body


async def test_reset_sets_the_new_password(api, mailbox, user_with_mailbox):
    await _forgot(api, user_with_mailbox["email"])
    token = token_from(mailbox.last_to(user_with_mailbox["email"]))

    response = await api.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": "brand-new-secret"}
    )

    assert response.status_code == 200
    login = await api.post(
        "/api/v1/auth/login",
        json={"email": user_with_mailbox["email"], "password": "brand-new-secret"},
    )
    assert login.status_code == 200


async def test_reset_signs_you_in(api, mailbox, user_with_mailbox):
    """Returning a token pair saves an immediate second login with a password
    the user has only just invented."""
    await _forgot(api, user_with_mailbox["email"])
    token = token_from(mailbox.last_to(user_with_mailbox["email"]))

    pair = (
        await api.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": "brand-new-secret"},
        )
    ).json()

    me = await api.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {pair['access_token']}"}
    )
    assert me.status_code == 200


async def test_the_old_password_stops_working(api, mailbox, user_with_mailbox):
    await _forgot(api, user_with_mailbox["email"])
    token = token_from(mailbox.last_to(user_with_mailbox["email"]))
    await api.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": "brand-new-secret"}
    )

    login = await api.post(
        "/api/v1/auth/login",
        json={
            "email": user_with_mailbox["email"],
            "password": user_with_mailbox["password"],
        },
    )
    assert login.status_code == 401


async def test_reset_revokes_existing_sessions(api, mailbox, user_with_mailbox):
    """Reset is what someone presses when they think an attacker is in the
    account. Leaving the attacker's session alive defeats the whole flow."""
    attacker_session = user_with_mailbox["tokens"]["refresh_token"]

    await _forgot(api, user_with_mailbox["email"])
    token = token_from(mailbox.last_to(user_with_mailbox["email"]))
    await api.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": "brand-new-secret"}
    )

    response = await api.post(
        "/api/v1/auth/refresh", json={"refresh_token": attacker_session}
    )
    assert response.status_code == 401


async def test_a_completed_reset_verifies_the_address(api, mailbox, user_with_mailbox):
    """Receiving the link proves control of the mailbox, which is exactly what
    verification asks for. A second link would be theatre."""
    await _forgot(api, user_with_mailbox["email"])
    token = token_from(mailbox.last_to(user_with_mailbox["email"]))

    pair = (
        await api.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": "brand-new-secret"},
        )
    ).json()

    me = await api.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {pair['access_token']}"}
    )
    assert me.json()["email_verified"] is True


# -- Token rules ---------------------------------------------------------------


async def test_a_link_works_only_once(api, mailbox, user_with_mailbox):
    await _forgot(api, user_with_mailbox["email"])
    token = token_from(mailbox.last_to(user_with_mailbox["email"]))
    body = {"token": token, "new_password": "brand-new-secret"}
    assert (await api.post("/api/v1/auth/reset-password", json=body)).status_code == 200

    again = await api.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": "another-one-here"}
    )
    assert again.status_code == 422


async def test_requesting_again_invalidates_the_previous_link(api, mailbox, user_with_mailbox):
    """Otherwise every resend leaves another live link in another inbox
    message, each good for its full lifetime."""
    await _forgot(api, user_with_mailbox["email"])
    first = token_from(mailbox.last_to(user_with_mailbox["email"]))
    await _forgot(api, user_with_mailbox["email"])
    second = token_from(mailbox.last_to(user_with_mailbox["email"]))

    assert first != second
    stale = await api.post(
        "/api/v1/auth/reset-password", json={"token": first, "new_password": "brand-new-secret"}
    )
    assert stale.status_code == 422
    fresh = await api.post(
        "/api/v1/auth/reset-password", json={"token": second, "new_password": "brand-new-secret"}
    )
    assert fresh.status_code == 200


async def test_an_expired_link_is_refused(api, mailbox, user_with_mailbox, db_session):
    from datetime import timedelta

    from sqlalchemy import select

    from app.core.time import utcnow
    from app.models.one_time_token import OneTimeToken

    await _forgot(api, user_with_mailbox["email"])
    token = token_from(mailbox.last_to(user_with_mailbox["email"]))

    row = (
        await db_session.execute(
            select(OneTimeToken).where(OneTimeToken.purpose == TokenPurpose.PASSWORD_RESET)
        )
    ).scalar_one()
    row.expires_at = utcnow() - timedelta(seconds=1)
    await db_session.flush()

    response = await api.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": "brand-new-secret"}
    )
    assert response.status_code == 422


async def test_a_made_up_token_is_refused(api):
    response = await api.post(
        "/api/v1/auth/reset-password",
        json={"token": "definitely-not-a-real-token", "new_password": "brand-new-secret"},
    )
    assert response.status_code == 422


async def test_a_verification_token_cannot_reset_a_password(api, mailbox, registered_user):
    """One table serves both purposes. A lookup that forgot to filter on
    purpose would let a signup link change a password."""
    verification = token_from(mailbox.last_to(registered_user["email"]))

    response = await api.post(
        "/api/v1/auth/reset-password",
        json={"token": verification, "new_password": "brand-new-secret"},
    )

    assert response.status_code == 422
    login = await api.post(
        "/api/v1/auth/login",
        json={"email": registered_user["email"], "password": registered_user["password"]},
    )
    assert login.status_code == 200


@pytest.mark.parametrize("password", ["short", ""])
async def test_a_weak_new_password_is_refused(api, mailbox, user_with_mailbox, password):
    await _forgot(api, user_with_mailbox["email"])
    token = token_from(mailbox.last_to(user_with_mailbox["email"]))

    response = await api.post(
        "/api/v1/auth/reset-password", json={"token": token, "new_password": password}
    )
    assert response.status_code == 422


async def test_the_raw_token_is_never_stored(api, mailbox, user_with_mailbox, db_session):
    """Only a hash is persisted, so a database leak yields nothing usable."""
    from sqlalchemy import select

    from app.models.one_time_token import OneTimeToken

    await _forgot(api, user_with_mailbox["email"])
    token = token_from(mailbox.last_to(user_with_mailbox["email"]))

    hashes = (
        await db_session.execute(
            select(OneTimeToken.token_hash).where(
                OneTimeToken.purpose == TokenPurpose.PASSWORD_RESET
            )
        )
    ).scalars().all()

    assert token not in hashes
    assert all(len(h) == 64 for h in hashes)
