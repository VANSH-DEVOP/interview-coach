"""The email seam.

Nothing here opens a socket. What is worth testing is the factory's refusals --
the configurations that must not be allowed to start -- and that the SMTP
backend builds a message the stdlib will accept rather than concatenating
headers by hand.
"""

import pytest

from app.core.config import get_settings
from app.services.email import get_email_sender
from app.services.email.base import EmailMessage
from app.services.email.console import LoggingEmailSender
from app.services.email.memory import RecordingEmailSender
from app.services.email.smtp import SmtpEmailSender


@pytest.fixture(autouse=True)
def _clear_factory_cache():
    get_email_sender.cache_clear()
    yield
    get_email_sender.cache_clear()


# -- Factory -------------------------------------------------------------------


def test_the_default_backend_is_the_logger(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "EMAIL_BACKEND", "log")
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")

    assert isinstance(get_email_sender(), LoggingEmailSender)


def test_the_logging_backend_is_refused_in_production(monkeypatch):
    """It prints reset links in full. Running it in production publishes
    account-takeover links to anyone who can read the logs, while every flow
    still reports success -- so this has to fail loudly, not warn."""
    settings = get_settings()
    monkeypatch.setattr(settings, "EMAIL_BACKEND", "log")
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")

    with pytest.raises(ValueError, match="not allowed in production"):
        get_email_sender()


def test_smtp_backend_is_built_from_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "EMAIL_BACKEND", "smtp")
    monkeypatch.setattr(settings, "SMTP_USE_TLS", False)
    monkeypatch.setattr(settings, "SMTP_START_TLS", True)

    assert isinstance(get_email_sender(), SmtpEmailSender)


def test_smtp_backend_is_allowed_in_production(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "EMAIL_BACKEND", "smtp")
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")

    assert isinstance(get_email_sender(), SmtpEmailSender)


def test_both_tls_modes_at_once_is_refused(monkeypatch):
    """aiosmtplib rejects it at send time, which would mean every email fails
    at runtime instead of the process refusing to start."""
    settings = get_settings()
    monkeypatch.setattr(settings, "EMAIL_BACKEND", "smtp")
    monkeypatch.setattr(settings, "SMTP_USE_TLS", True)
    monkeypatch.setattr(settings, "SMTP_START_TLS", True)

    with pytest.raises(ValueError, match="mutually exclusive"):
        get_email_sender()


# -- Backends ------------------------------------------------------------------


async def test_the_logging_backend_writes_the_message(caplog):
    import logging as stdlib_logging

    caplog.set_level(stdlib_logging.INFO)

    await LoggingEmailSender().send(
        EmailMessage(to="someone@example.com", subject="Hello", body="Body text.")
    )

    # The body has to be there: reading the reset link out of the console is
    # the entire reason this backend exists.
    assert "someone@example.com" in caplog.text
    assert "Body text." in caplog.text


async def test_the_recording_backend_captures_messages():
    sender = RecordingEmailSender()

    await sender.send(EmailMessage(to="a@example.com", subject="First", body="one"))
    await sender.send(EmailMessage(to="a@example.com", subject="Second", body="two"))
    await sender.send(EmailMessage(to="b@example.com", subject="Other", body="three"))

    assert len(sender.sent) == 3
    # Most recent wins: a resend should be what the assertion sees.
    assert sender.last_to("a@example.com").subject == "Second"
    assert sender.last_to("nobody@example.com") is None


async def test_smtp_failures_become_email_errors(monkeypatch):
    """Callers must never need to know which library raised."""
    import aiosmtplib

    from app.services.email.base import EmailError

    async def boom(*args, **kwargs):
        raise aiosmtplib.SMTPConnectError("no route to host")

    monkeypatch.setattr(aiosmtplib, "send", boom)
    sender = SmtpEmailSender(
        host="smtp.example.com",
        port=587,
        username=None,
        password=None,
        sender="no-reply@example.com",
        use_tls=False,
        start_tls=True,
        timeout=5,
    )

    with pytest.raises(EmailError):
        await sender.send(EmailMessage(to="a@example.com", subject="s", body="b"))


async def test_a_newline_in_a_header_is_refused_and_nothing_is_sent(monkeypatch):
    """Header injection: a newline in a subject or address would otherwise add
    headers of the attacker's choosing, Bcc being the obvious one.

    Built via email.message rather than string concatenation, which refuses
    these outright. The point of this test is that the refusal arrives as an
    EmailError and that no message goes out -- an unhandled ValueError escaping
    a mail backend would be a 500 on a flow that is supposed to fail quietly.
    """
    import aiosmtplib

    from app.services.email.base import EmailError

    sent: list = []

    async def capture(mime, **kwargs):
        sent.append(mime)

    monkeypatch.setattr(aiosmtplib, "send", capture)
    sender = SmtpEmailSender(
        host="smtp.example.com",
        port=587,
        username=None,
        password=None,
        sender="no-reply@example.com",
        use_tls=False,
        start_tls=True,
        timeout=5,
    )

    with pytest.raises(EmailError):
        await sender.send(
            EmailMessage(
                to="a@example.com",
                subject="Reset\nBcc: attacker@example.com",
                body="body",
            )
        )

    assert sent == [], "a message with an injected header reached the transport"


async def test_a_normal_message_reaches_the_transport(monkeypatch):
    """The counterpart to the test above: the refusal must be specific to bad
    headers, not something that rejects ordinary mail too."""
    import aiosmtplib

    sent: list = []

    async def capture(mime, **kwargs):
        sent.append(mime)

    monkeypatch.setattr(aiosmtplib, "send", capture)
    sender = SmtpEmailSender(
        host="smtp.example.com",
        port=587,
        username=None,
        password=None,
        sender="no-reply@example.com",
        use_tls=False,
        start_tls=True,
        timeout=5,
    )

    await sender.send(
        EmailMessage(to="a@example.com", subject="Reset your password", body="link")
    )

    assert len(sent) == 1
    assert sent[0]["To"] == "a@example.com"
    assert sent[0]["Subject"] == "Reset your password"
    assert sent[0]["From"] == "no-reply@example.com"
