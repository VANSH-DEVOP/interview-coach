"""Email verification.

Deliberately toothless: verifying is recorded and surfaced, and gates nothing.
The default email backend writes to a log, so gating login would lock everyone
out of a local or demo deployment. Several tests below pin that down, because
"add a check for email_verified" is an easy and very disruptive change to make
without noticing what it breaks.
"""

import uuid

from tests.conftest import token_from


async def _register(api, email=None):
    email = email or f"verify-{uuid.uuid4().hex[:10]}@example.com"
    password = "correct-horse-battery"
    response = await api.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Verify Me"},
    )
    assert response.status_code == 201, response.text
    return {"email": email, "password": password, "user": response.json()}


# -- Registration sends the email ----------------------------------------------


async def test_registering_sends_a_verification_email(api, mailbox):
    user = await _register(api)

    message = mailbox.last_to(user["email"])
    assert message is not None
    assert "http://localhost:3000/verify-email?token=" in message.body


async def test_a_new_account_starts_unverified(api, mailbox):
    user = await _register(api)
    assert user["user"]["email_verified"] is False


async def test_registration_succeeds_even_if_the_email_fails(api, db_session, monkeypatch):
    """A mail server having a bad afternoon must not fail an account creation
    that otherwise worked -- the address would be taken and unusable."""
    from app.api.deps import get_account_service
    from app.repositories.one_time_token_repository import OneTimeTokenRepository
    from app.repositories.user_repository import UserRepository
    from app.services.account_service import AccountService
    from app.services.email.base import EmailError, EmailSender
    from app.services.one_time_tokens import OneTimeTokenService

    class BrokenSender(EmailSender):
        async def send(self, message):
            raise EmailError("the mail server is on fire")

    from app.main import app

    app.dependency_overrides[get_account_service] = lambda: AccountService(
        UserRepository(db_session),
        OneTimeTokenService(OneTimeTokenRepository(db_session)),
        BrokenSender(),
    )
    try:
        user = await _register(api)
        assert user["user"]["email_verified"] is False
    finally:
        app.dependency_overrides.pop(get_account_service, None)


# -- Verifying -----------------------------------------------------------------


async def test_the_link_verifies_the_address(api, mailbox):
    user = await _register(api)
    token = token_from(mailbox.last_to(user["email"]))

    response = await api.post("/api/v1/auth/verify-email", json={"token": token})

    assert response.status_code == 200
    assert response.json()["email_verified"] is True


async def test_verification_needs_no_login(api, mailbox):
    """The link is opened from an inbox, quite possibly on another device."""
    user = await _register(api)
    token = token_from(mailbox.last_to(user["email"]))

    response = await api.post("/api/v1/auth/verify-email", json={"token": token})

    assert response.status_code == 200


async def test_the_state_is_visible_on_the_profile(api, mailbox):
    user = await _register(api)
    token = token_from(mailbox.last_to(user["email"]))
    await api.post("/api/v1/auth/verify-email", json={"token": token})

    login = await api.post(
        "/api/v1/auth/login", json={"email": user["email"], "password": user["password"]}
    )
    me = await api.get(
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    assert me.json()["email_verified"] is True


async def test_a_link_works_only_once(api, mailbox):
    user = await _register(api)
    token = token_from(mailbox.last_to(user["email"]))
    assert (await api.post("/api/v1/auth/verify-email", json={"token": token})).status_code == 200

    again = await api.post("/api/v1/auth/verify-email", json={"token": token})
    assert again.status_code == 422


async def test_a_made_up_token_is_refused(api):
    response = await api.post("/api/v1/auth/verify-email", json={"token": "nope"})
    assert response.status_code == 422


async def test_a_reset_token_cannot_verify_an_address(api, mailbox, registered_user):
    """The mirror of the crossover test in test_password_reset_api."""
    mailbox.clear()
    await api.post("/api/v1/auth/forgot-password", json={"email": registered_user["email"]})
    reset_token = token_from(mailbox.last_to(registered_user["email"]))

    response = await api.post("/api/v1/auth/verify-email", json={"token": reset_token})

    assert response.status_code == 422


# -- Resending -----------------------------------------------------------------


async def test_resend_sends_another_email(api, mailbox, registered_user):
    mailbox.clear()

    response = await api.post(
        "/api/v1/auth/resend-verification", headers=registered_user["headers"]
    )

    assert response.status_code == 202
    assert mailbox.last_to(registered_user["email"]) is not None


async def test_resending_invalidates_the_previous_link(api, mailbox, registered_user):
    first = token_from(mailbox.last_to(registered_user["email"]))
    await api.post("/api/v1/auth/resend-verification", headers=registered_user["headers"])
    second = token_from(mailbox.last_to(registered_user["email"]))

    assert first != second
    assert (await api.post("/api/v1/auth/verify-email", json={"token": first})).status_code == 422
    assert (await api.post("/api/v1/auth/verify-email", json={"token": second})).status_code == 200


async def test_resend_requires_authentication(api):
    assert (await api.post("/api/v1/auth/resend-verification")).status_code == 401


async def test_resend_is_a_no_op_once_verified(api, mailbox, registered_user):
    token = token_from(mailbox.last_to(registered_user["email"]))
    await api.post("/api/v1/auth/verify-email", json={"token": token})
    mailbox.clear()

    response = await api.post(
        "/api/v1/auth/resend-verification", headers=registered_user["headers"]
    )

    assert response.status_code == 202
    assert mailbox.sent == [], "sent a confirmation email for an already-confirmed address"


# -- Verification gates nothing ------------------------------------------------


async def test_an_unverified_user_can_log_in(api, mailbox):
    user = await _register(api)

    response = await api.post(
        "/api/v1/auth/login", json={"email": user["email"], "password": user["password"]}
    )

    assert response.status_code == 200


async def test_an_unverified_user_can_use_the_app(api, mailbox):
    """The default backend writes email to a log. Gating features on
    verification would make a fresh local or demo deployment unusable."""
    user = await _register(api)
    login = await api.post(
        "/api/v1/auth/login", json={"email": user["email"], "password": user["password"]}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    created = await api.post(
        "/api/v1/interviews", json={"title": "Unverified", "question_count": 3}, headers=headers
    )

    assert created.status_code == 201
