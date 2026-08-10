"""The identity actually reaches the services that send text to the provider.

The boundary redacts patterns whether or not anyone plumbs an identity, so
dropping `current_user` from a dependency would not fail a single other test --
it would just quietly stop redacting names. These tests exist to make that
regression loud.
"""

import uuid

from app.api.deps import get_interview_service, get_resume_service
from app.models.user import User
from app.services.ai.masking import default_redactor


def _user(full_name: str = "Ada Lovelace") -> User:
    return User(
        id=uuid.uuid4(),
        email="ada@example.com",
        hashed_password="x",
        full_name=full_name,
        is_active=True,
    )


def test_resume_service_is_wired_with_the_account_holders_name(storage_root) -> None:
    service = get_resume_service(resumes=object(), chunks=object(), current_user=_user())

    assert service.redactor is not None
    assert "[REDACTED_NAME]" in service.redactor.redact("Ada Lovelace")


def test_interview_service_construction_accepts_the_current_user() -> None:
    service = get_interview_service(
        interviews=object(), resumes=object(), reports=object(), current_user=_user()
    )

    # The redactor lives inside the generator, which is the static one without
    # a Gemini key, so assert the wiring rather than reaching through it.
    assert service.question_generator is not None


def test_a_user_with_no_name_falls_back_to_patterns_only(storage_root) -> None:
    """full_name is nullable; that must degrade, not crash."""
    service = get_resume_service(
        resumes=object(), chunks=object(), current_user=_user(full_name=None)
    )

    assert service.redactor is default_redactor()
    assert "[REDACTED_EMAIL]" in service.redactor.redact("ada@example.com")
