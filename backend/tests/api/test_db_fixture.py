"""The transactional test-database fixture itself.

If isolation leaks, every other database test becomes order-dependent and
mysteriously flaky, so the fixture's own guarantees are worth asserting
directly rather than inferring from downstream failures.
"""

import uuid

from sqlalchemy import select

from app.models.user import User


async def test_schema_comes_from_migrations(db_session):
    # A column added by migration 0002 -- present only if the migration ran,
    # which is the point of building the schema from alembic rather than
    # Base.metadata.create_all.
    from app.models.interview_session import InterviewSession

    result = await db_session.execute(select(InterviewSession).limit(1))
    assert result.all() == []
    assert hasattr(InterviewSession, "interview_type")


async def test_writes_are_visible_within_a_test(db_session):
    email = f"visible-{uuid.uuid4().hex[:8]}@example.com"
    db_session.add(User(email=email, hashed_password="x", full_name="A"))
    await db_session.flush()

    found = (
        await db_session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    assert found is not None


# The two tests below are a pair: the first writes and commits, the second
# asserts the write is gone. If the rollback in the fixture ever stops working,
# the second fails.

_LEAK_EMAIL = "rollback-probe@example.com"


async def test_a_commit_inside_a_test_is_not_durable(db_session):
    db_session.add(User(email=_LEAK_EMAIL, hashed_password="x", full_name="A"))
    # An explicit commit, exactly as get_session does per request. The savepoint
    # join means this succeeds without escaping the outer transaction.
    await db_session.commit()

    found = (
        await db_session.execute(select(User).where(User.email == _LEAK_EMAIL))
    ).scalar_one_or_none()
    assert found is not None, "the commit should be visible inside its own test"


async def test_the_previous_tests_commit_was_rolled_back(db_session):
    found = (
        await db_session.execute(select(User).where(User.email == _LEAK_EMAIL))
    ).scalar_one_or_none()
    assert found is None, "state leaked between tests; the fixture is not isolating"


async def test_api_fixture_reaches_the_database(api):
    response = await api.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["database"] == "up"


async def test_api_fixture_disables_ai_and_rate_limiting(api):
    from app.core.config import get_settings

    settings = get_settings()
    assert settings.GEMINI_API_KEY is None
    assert settings.RATE_LIMIT_ENABLED is False


async def test_registered_user_fixture_provides_working_credentials(api, registered_user):
    response = await api.get("/api/v1/users/me", headers=registered_user["headers"])
    assert response.status_code == 200
    assert response.json()["email"] == registered_user["email"]


async def test_the_suite_cannot_send_real_email(api, registered_user):
    """No mail leaves this process, whatever `.env` says.

    The `mailbox` fixture swaps in a recording sender, but it is opt-in -- so
    every test that did not ask for it used whatever EMAIL_BACKEND was
    configured. Once real SMTP credentials existed in the repository-root
    `.env`, and once Settings started reading that file from any working
    directory, that meant a live Gmail account: a suite run registers dozens of
    users and fires a verification email at each `@example.com` address, none
    of which exist.

    Guarded the same way GEMINI_API_KEY is, and asserted here rather than
    trusted, because the failure is invisible from inside the suite -- every
    test still passes while the mail goes out.
    """
    from app.core.config import get_settings
    from app.services.email import LoggingEmailSender, get_email_sender

    settings = get_settings()
    assert settings.EMAIL_BACKEND == "log"
    assert isinstance(get_email_sender(), LoggingEmailSender)
    # And registration -- which sends a verification email -- really happened.
    assert registered_user["user"]["email"].endswith("@example.com")
