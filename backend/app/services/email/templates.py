"""The messages this application sends.

Plain text, short, one link each. Kept together so the wording and the links
are reviewable in one place rather than spread through the auth service.

Links are absolute and built from `FRONTEND_BASE_URL`, never from the incoming
request: a Host header is attacker-controlled, and deriving a password-reset
link from it is how you email someone a link that posts their new password to
somebody else's server.
"""

from urllib.parse import quote

from app.core.config import get_settings
from app.services.email.base import EmailMessage


def _link(path: str, token: str) -> str:
    base = get_settings().FRONTEND_BASE_URL.rstrip("/")
    # The token is urlsafe-base64, which contains `-` and `_` but never `+` or
    # `/`. Quoted anyway: relying on the generator's alphabet to keep a URL
    # well-formed is a dependency nobody remembers when the generator changes.
    return f"{base}{path}?token={quote(token, safe='')}"


def password_reset(to: str, token: str, *, valid_for: str) -> EmailMessage:
    link = _link("/reset-password", token)
    return EmailMessage(
        to=to,
        subject="Reset your InterviewPilot password",
        body=(
            "Someone asked to reset the password for this account.\n\n"
            f"{link}\n\n"
            f"The link works once and expires in {valid_for}.\n\n"
            "If this wasn't you, nothing has changed and you can ignore this "
            "email. Your password is still the one you already had."
        ),
    )


def email_verification(to: str, token: str, *, valid_for: str) -> EmailMessage:
    link = _link("/verify-email", token)
    return EmailMessage(
        to=to,
        subject="Confirm your InterviewPilot email address",
        body=(
            "Welcome to InterviewPilot. Confirm this address to finish setting "
            "up your account:\n\n"
            f"{link}\n\n"
            f"The link works once and expires in {valid_for}.\n\n"
            "If you didn't create this account, you can ignore this email."
        ),
    )
